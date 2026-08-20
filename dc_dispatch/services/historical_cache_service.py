from __future__ import annotations

import base64
import hashlib
import json
import zlib
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

import dc_dispatch.services.run_service as rs
from dc_dispatch.services import history_policy_service as history
from dc_dispatch.services import size_service
from dc_dispatch.services import forecast_service


CACHE_VERSION = 1


def _pack(value):
    raw = json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.b64encode(
        zlib.compress(raw, level=6)
    ).decode("ascii")


def _unpack(value, default):
    if not value:
        return default

    raw = zlib.decompress(
        base64.b64decode(value)
    )
    return json.loads(
        raw.decode("utf-8")
    )


def _stores(run):
    return sorted(
        {
            row.store_warehouse
            for row in run.store_rules
            if row.store_warehouse
        }
    )


def cache_input_hash(run):
    settings = frappe.get_single(
        "DC Dispatch Settings"
    )
    groups = (
        size_service.size_group_configuration(
            settings
        )
    )

    payload = {
        "cache_version": CACHE_VERSION,
        "company": run.company,
        "sales_from_date": str(
            run.sales_from_date or ""
        ),
        "sales_to_date": str(
            run.sales_to_date or ""
        ),
        "stores": _stores(run),
        "size_attribute": (
            size_service.size_attribute_name(
                settings
            )
        ),
        "size_groups": {
            group: sorted(values)
            for group, values in groups.items()
        },
    }

    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _variant_sales_rows(run, stores):
    if not stores:
        return []

    return frappe.db.sql(
        """
        SELECT
            COALESCE(
                NULLIF(item.variant_of, ''),
                item.name
            ) AS item_template,
            sii.item_code,
            sii.warehouse AS store_warehouse,
            SUM(sii.qty) AS gross_sales
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si
            ON si.name = sii.parent
           AND si.docstatus = 1
        INNER JOIN `tabItem` item
            ON item.name = sii.item_code
        WHERE si.company = %(company)s
          AND si.is_return = 0
          AND si.posting_date
              BETWEEN %(from_date)s
                  AND %(to_date)s
          AND sii.warehouse IN %(stores)s
        GROUP BY
            COALESCE(
                NULLIF(item.variant_of, ''),
                item.name
            ),
            sii.item_code,
            sii.warehouse
        """,
        {
            "company": run.company,
            "from_date": run.sales_from_date,
            "to_date": run.sales_to_date,
            "stores": tuple(stores),
        },
        as_dict=True,
    )


def build_cache(run):
    """Scan historical transactions once and persist reusable aggregates."""
    rs._require_saved(run)

    stores = _stores(run)
    if not stores:
        frappe.throw(
            _("Load eligible stores first.")
        )

    sales_rows = _variant_sales_rows(
        run,
        stores,
    )
    resolved_returns = (
        history._return_rows(
            run,
            stores,
        )
    )

    net_by_variant_store = defaultdict(
        float
    )

    for row in sales_rows:
        key = (
            row.item_template,
            row.item_code,
            row.store_warehouse,
        )
        net_by_variant_store[key] += flt(
            row.gross_sales
        )

    for row in resolved_returns:
        if (
            row.return_classification
            != "Same-Store Return - Deducted"
        ):
            continue

        key = (
            row.item_template,
            row.item_code,
            row.return_store_warehouse,
        )
        net_by_variant_store[key] -= flt(
            row.return_qty
        )

    item_codes = {
        item_code
        for _template, item_code, _store
        in net_by_variant_store
        if item_code
    }

    settings = frappe.get_single(
        "DC Dispatch Settings"
    )
    size_group_by_item = (
        size_service.variant_size_group_map(
            item_codes,
            settings=settings,
        )
    )

    demand = defaultdict(float)
    size_demand = defaultdict(float)

    for (
        template,
        item_code,
        store,
    ), quantity in (
        net_by_variant_store.items()
    ):
        demand[
            (template, store)
        ] += flt(quantity)

        group = size_group_by_item.get(
            item_code
        )
        if group:
            positive = max(
                0.0,
                flt(quantity),
            )
            if positive:
                size_demand[
                    (
                        template,
                        store,
                        group,
                    )
                ] += positive

    demand_rows = [
        [
            template,
            store,
            quantity,
        ]
        for (
            template,
            store,
        ), quantity in demand.items()
        if flt(quantity) != 0
    ]

    size_rows = [
        [
            template,
            store,
            group,
            quantity,
        ]
        for (
            template,
            store,
            group,
        ), quantity in size_demand.items()
        if flt(quantity) > 0
    ]

    candidates = sorted(
        {
            template
            for (
                template,
                _store,
                quantity,
            ) in demand_rows
            if flt(quantity) != 0
        }
    )

    cache_name = run.name
    if frappe.db.exists(
        "DC Dispatch Historical Cache",
        cache_name,
    ):
        cache = frappe.get_doc(
            "DC Dispatch Historical Cache",
            cache_name,
        )
    else:
        cache = frappe.new_doc(
            "DC Dispatch Historical Cache"
        )
        cache.run = run.name

    cache.input_hash = (
        cache_input_hash(run)
    )
    cache.built_at = now_datetime()
    cache.source_row_count = len(
        sales_rows
    )
    cache.return_row_count = len(
        resolved_returns
    )
    cache.candidate_template_count = len(
        candidates
    )
    cache.demand_data = _pack(
        demand_rows
    )
    cache.size_data = _pack(
        size_rows
    )
    cache.candidate_templates_data = (
        _pack(candidates)
    )
    cache.save(
        ignore_permissions=True
    )

    return load_cache_data(
        run,
        require_valid=True,
    )


def load_cache_data(
    run,
    require_valid=True,
):
    cache_name = run.name

    if not frappe.db.exists(
        "DC Dispatch Historical Cache",
        cache_name,
    ):
        if require_valid:
            frappe.throw(
                _(
                    "Historical Analysis Cache is missing. "
                    "Run Check Store History first."
                )
            )
        return None

    cache = frappe.get_doc(
        "DC Dispatch Historical Cache",
        cache_name,
    )

    current_hash = (
        cache_input_hash(run)
    )
    if (
        cache.input_hash
        != current_hash
    ):
        if require_valid:
            frappe.throw(
                _(
                    "Historical inputs changed after the last analysis. "
                    "Run Check Store History again."
                )
            )
        return None

    demand_rows = _unpack(
        cache.demand_data,
        [],
    )
    size_rows = _unpack(
        cache.size_data,
        [],
    )
    candidates = set(
        _unpack(
            cache.candidate_templates_data,
            [],
        )
    )

    demand = {
        (
            template,
            store,
        ): flt(quantity)
        for (
            template,
            store,
            quantity,
        ) in demand_rows
    }

    size_by_template = defaultdict(
        lambda: defaultdict(float)
    )

    for (
        template,
        store,
        group,
        quantity,
    ) in size_rows:
        size_by_template[
            template
        ][
            (
                store,
                group,
            )
        ] += flt(quantity)

    return {
        "input_hash": (
            cache.input_hash
        ),
        "built_at": cache.built_at,
        "demand": demand,
        "candidate_templates": (
            candidates
        ),
        "size_by_template": {
            template: dict(values)
            for (
                template,
                values,
            ) in (
                size_by_template.items()
            )
        },
        "source_row_count": int(
            cache.source_row_count or 0
        ),
        "return_row_count": int(
            cache.return_row_count or 0
        ),
        "candidate_template_count": int(
            cache.candidate_template_count
            or 0
        ),
    }


def history_result_from_cache(run):
    """Apply current historical scope to cached raw history; no invoice scan."""
    rs._require_saved(run)

    if not run.store_rules:
        frappe.throw(
            _("Load eligible stores first.")
        )

    cache = load_cache_data(
        run,
        require_valid=True,
    )
    candidate_templates = set(
        cache["candidate_templates"]
    )

    settings = frappe.get_single(
        "DC Dispatch Settings"
    )
    value_fields = (
        history.historical_filter_fieldnames(
            run
        )
        | {
            settings.item_main_group_field,
        }
    )

    template_values = (
        rs._item_values(
            candidate_templates,
            value_fields,
        )
        if candidate_templates
        else {}
    )

    allowed_templates = (
        history._allowed_templates_for_history_check(
            run,
            candidate_templates,
            template_values,
        )
    )

    totals = defaultdict(float)

    for (
        template,
        store,
    ), quantity in (
        cache["demand"].items()
    ):
        if (
            template
            not in allowed_templates
        ):
            continue

        totals[store] += max(
            0.0,
            flt(quantity),
        )

    result_rows = []
    no_history = []

    for row in run.store_rules:
        own_demand = max(
            0.0,
            totals[
                row.store_warehouse
            ],
        )

        if own_demand > 0:
            history_status = (
                "Has History"
            )
        else:
            history_status = (
                "No History"
            )

            if (
                row.decision
                == "Include"
            ):
                no_history.append(
                    row.store_warehouse
                )
            elif (
                row.decision
                == "Use Reference Store"
                and max(
                    0.0,
                    totals[
                        row.reference_store
                    ],
                )
                <= 0
            ):
                history_status = (
                    "Reference Store Missing"
                )
                no_history.append(
                    row.store_warehouse
                )

        result_rows.append(
            {
                "store": (
                    row.store_warehouse
                ),
                "demand_units": (
                    own_demand
                ),
                "net_units": (
                    own_demand
                ),
                "history_status": (
                    history_status
                ),
            }
        )

    return {
        "stores": result_rows,
        "no_history": no_history,
        "cache_built_at": (
            cache["built_at"]
        ),
        "cache_source_rows": (
            cache[
                "source_row_count"
            ]
        ),
        "cache_return_rows": (
            cache[
                "return_row_count"
            ]
        ),
    }


def apply_history_result(
    run,
    result,
):
    by_store = {
        row["store"]: row
        for row in result.get(
            "stores",
            [],
        )
    }

    for row in run.store_rules:
        values = by_store.get(
            row.store_warehouse,
            {},
        )
        row.history_status = (
            values.get(
                "history_status"
            )
            or "No History"
        )
        row.historical_demand_qty = flt(
            values.get(
                "demand_units",
                0,
            )
        )

    no_history = result.get(
        "no_history",
        [],
    )

    forecast_service.recalculate_final_demands(
        run
    )

    if (
        no_history
        and run.status
        in {
            "Draft",
            "Items Loaded",
            "Reference Review Required",
        }
    ):
        run.status = (
            "Reference Review Required"
        )
    elif (
        not no_history
        and run.status
        == "Reference Review Required"
    ):
        run.status = "Items Loaded"

    run.save()
    return result
