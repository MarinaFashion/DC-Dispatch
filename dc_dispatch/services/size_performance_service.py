from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from math import floor

import frappe
from frappe import _
from frappe.utils import cint, flt

from dc_dispatch.services.allocation import (
    StyleAllocation,
    TIER_ORDER,
    allocate_integer_with_caps,
    allocate_style,
)
from dc_dispatch.services import size_service


GROUPS = ("Small", "Medium", "Large")


def validate_size_factor_inputs(run):
    weight = flt(getattr(run, "size_performance_weight", 0))
    enabled = cint(getattr(run, "include_size_performance_factor", 0))

    if weight < 0 or weight > 100:
        frappe.throw(
            _("Size Performance Weight % must be between 0 and 100.")
        )

    if enabled and weight <= 0:
        frappe.throw(
            _(
                "Enter a Size Performance Weight greater than 0, "
                "or clear Include Size Performance Factor."
            )
        )

    if enabled:
        settings = frappe.get_single("DC Dispatch Settings")
        errors = size_service.validate_size_group_configuration(settings)
        if errors:
            frappe.throw("<br>".join(errors[:20]))


def configuration_signature(run):
    enabled = bool(
        cint(getattr(run, "include_size_performance_factor", 0))
    )

    if not enabled:
        payload = {"enabled": False}
    else:
        settings = frappe.get_single("DC Dispatch Settings")
        groups = size_service.size_group_configuration(settings)
        payload = {
            "enabled": True,
            "weight": flt(
                getattr(run, "size_performance_weight", 0)
            ),
            "size_attribute": size_service.size_attribute_name(settings),
            "groups": {
                group: sorted(values)
                for group, values in groups.items()
            },
        }

    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def assert_size_configuration_unchanged(run):
    current = configuration_signature(run)
    stored = str(
        getattr(run, "size_performance_signature", "") or ""
    )

    # Backward compatibility with proposals calculated before v0.6.0
    # when the Size Performance Factor is disabled.
    if (
        not stored
        and not cint(
            getattr(
                run,
                "include_size_performance_factor",
                0,
            )
        )
    ):
        return

    if not stored or stored != current:
        frappe.throw(
            _(
                "Size Performance settings changed after calculation. "
                "Recalculate the proposal before continuing."
            )
        )


def _gross_size_sales_rows(run, stores, historical_templates):
    """Load only size-level sales belonging to cohorts actually used.

    v0.6.0 loaded every historical variant/store row in the date range.
    v0.6.1 limits the SQL extraction to the union of historical templates
    selected by the current run's cohorts.
    """
    stores = list(stores or [])
    templates = sorted(set(historical_templates or []))
    if not stores or not templates:
        return []

    return frappe.db.sql(
        """
        SELECT
            COALESCE(NULLIF(item.variant_of, ''), item.name)
                AS item_template,
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
              BETWEEN %(from_date)s AND %(to_date)s
          AND sii.warehouse IN %(stores)s
          AND COALESCE(NULLIF(item.variant_of, ''), item.name)
              IN %(templates)s
        GROUP BY
            COALESCE(NULLIF(item.variant_of, ''), item.name),
            sii.item_code,
            sii.warehouse
        """,
        {
            "company": run.company,
            "from_date": run.sales_from_date,
            "to_date": run.sales_to_date,
            "stores": tuple(stores),
            "templates": tuple(templates),
        },
        as_dict=True,
    )


def build_size_context(
    run,
    stores,
    target_item_codes,
    historical_templates,
    resolved_returns,
):
    """Build one reusable, pre-aggregated size history index.

    The expensive work happens once per proposal:
      1. one size-level sales SQL query, already restricted to used cohorts;
      2. reuse the main calculation's already-resolved return rows;
      3. one bulk Item Variant -> Size Group mapping;
      4. pre-aggregate to Template -> (Store, Size Group) -> Qty.

    profile_for_cohort() therefore never scans the complete historical
    variant dataset again.
    """
    validate_size_factor_inputs(run)

    if not cint(
        getattr(run, "include_size_performance_factor", 0)
    ):
        return None

    settings = frappe.get_single("DC Dispatch Settings")
    historical_templates = set(historical_templates or [])

    gross_rows = _gross_size_sales_rows(
        run,
        stores,
        historical_templates,
    )

    relevant_returns = [
        row
        for row in (resolved_returns or [])
        if (
            row.item_template in historical_templates
            and row.return_classification
            == "Same-Store Return - Deducted"
        )
    ]

    historical_item_codes = {
        row.item_code
        for row in gross_rows
        if row.item_code
    }
    historical_item_codes.update(
        row.item_code
        for row in relevant_returns
        if row.item_code
    )

    all_item_codes = (
        historical_item_codes
        | set(target_item_codes or [])
    )

    group_by_item = size_service.variant_size_group_map(
        all_item_codes,
        settings=settings,
    )

    # Net demand at variant/store level follows the same rule as the
    # main proposal: Gross Sales - Same-Store Returns.
    net_by_variant_store = defaultdict(float)

    for row in gross_rows:
        key = (
            row.item_template,
            row.item_code,
            row.store_warehouse,
        )
        net_by_variant_store[key] += flt(row.gross_sales)

    for row in relevant_returns:
        key = (
            row.item_template,
            row.item_code,
            row.return_store_warehouse,
        )
        net_by_variant_store[key] -= flt(row.return_qty)

    # Main performance fix:
    # Collapse potentially many variant rows into at most Store x 3 groups
    # for each historical template before any target-style loop begins.
    by_template = defaultdict(lambda: defaultdict(float))

    for (
        template,
        item_code,
        store,
    ), quantity in net_by_variant_store.items():
        group = group_by_item.get(item_code)
        if not group:
            continue

        quantity = max(0.0, flt(quantity))
        if quantity <= 0:
            continue

        by_template[template][(store, group)] += quantity

    return {
        "by_template": {
            template: dict(values)
            for template, values in by_template.items()
        },
        "group_by_item": group_by_item,
        "stores": list(stores),
        "settings": settings,
        "profile_cache": {},
        "gross_rows": len(gross_rows),
        "aggregated_templates": len(by_template),
    }


def profile_for_cohort(run, cohort_templates, context):
    """Return relative size performance using only indexed cohort templates."""
    if not context:
        return None

    cohort_templates = frozenset(cohort_templates or [])
    if not cohort_templates:
        return None

    cache = context["profile_cache"]
    if cohort_templates in cache:
        return cache[cohort_templates]

    demand_by_store_group = defaultdict(float)
    group_totals = defaultdict(float)
    store_totals = defaultdict(float)

    # v0.6.0 scanned every historical variant/store record here for
    # every target style. v0.6.1 touches only the already aggregated
    # records of templates in this cohort.
    for template in cohort_templates:
        for (
            store,
            group,
        ), quantity in context["by_template"].get(
            template,
            {},
        ).items():
            demand_by_store_group[(store, group)] += quantity
            group_totals[group] += quantity
            store_totals[store] += quantity

    mapped_units = sum(group_totals.values())
    if mapped_units <= 0:
        cache[cohort_templates] = None
        return None

    indices = {}

    for store in context["stores"]:
        store_total = max(0.0, store_totals.get(store, 0))
        overall_store_share = (
            store_total / mapped_units
            if mapped_units
            else 0
        )

        for group in GROUPS:
            group_total = max(
                0.0,
                group_totals.get(group, 0),
            )
            store_group = max(
                0.0,
                demand_by_store_group.get(
                    (store, group),
                    0,
                ),
            )

            if (
                group_total <= 0
                or overall_store_share <= 0
            ):
                index = 1.0
            else:
                group_store_share = store_group / group_total
                index = (
                    group_store_share
                    / overall_store_share
                )

            indices[(store, group)] = max(
                0.0,
                flt(index),
            )

    # A no-history store inherits the relative size behavior of the same
    # reference store used by the main demand calculation.
    for rule in run.store_rules:
        if (
            rule.decision != "Use Reference Store"
            or not rule.reference_store
        ):
            continue

        for group in GROUPS:
            indices[
                (rule.store_warehouse, group)
            ] = indices.get(
                (rule.reference_store, group),
                1.0,
            )

    network_group_shares = {
        group: (
            max(
                0.0,
                group_totals.get(group, 0),
            )
            / mapped_units
        )
        for group in GROUPS
    }

    profile = {
        "indices": indices,
        "network_group_shares": network_group_shares,
        "mapped_units": mapped_units,
    }
    cache[cohort_templates] = profile
    return profile


def _fixed_minimums(baseline, stores, variants):
    fixed = {
        store.warehouse: {
            variant: 0
            for variant in variants
        }
        for store in stores
    }

    for store in stores:
        minimum = max(
            0,
            int(store.minimum_per_variant or 0),
        )
        if minimum <= 0:
            continue

        row = baseline.quantities.get(
            store.warehouse,
            {},
        )

        if all(
            int(row.get(variant, 0)) >= minimum
            for variant in variants
        ):
            for variant in variants:
                fixed[store.warehouse][variant] = minimum

    return fixed


def _variant_room(store, quantities, variant):
    maximum = max(
        0,
        int(store.maximum_per_style or 0),
    )

    if maximum == 0:
        return 10**9

    return max(
        0,
        maximum
        - int(
            quantities[
                store.warehouse
            ][variant]
        ),
    )


def _desired_variant_additions(
    remaining_stock,
    remaining_total,
    group_by_item,
    network_group_shares,
    weight,
):
    if remaining_total <= 0:
        return {
            variant: 0
            for variant in remaining_stock
        }

    physical_total = sum(remaining_stock.values())
    if physical_total <= 0:
        return {
            variant: 0
            for variant in remaining_stock
        }

    stock_by_group = defaultdict(int)
    other_variants = []

    for variant, quantity in remaining_stock.items():
        group = group_by_item.get(variant)
        if group:
            stock_by_group[group] += quantity
        else:
            other_variants.append(variant)

    mapped_stock = sum(stock_by_group.values())
    mapped_share = (
        mapped_stock / physical_total
        if physical_total
        else 0
    )

    available_groups = [
        group
        for group in GROUPS
        if stock_by_group.get(group, 0) > 0
    ]

    historical_total = sum(
        max(
            0.0,
            flt(
                network_group_shares.get(
                    group,
                    0,
                )
            ),
        )
        for group in available_groups
    )

    group_weights = {}

    for group in available_groups:
        stock_share = (
            stock_by_group[group]
            / physical_total
        )

        if historical_total > 0:
            historical_share = (
                mapped_share
                * max(
                    0.0,
                    flt(
                        network_group_shares.get(
                            group,
                            0,
                        )
                    ),
                )
                / historical_total
            )
        else:
            historical_share = stock_share

        group_weights[group] = (
            (1 - weight) * stock_share
            + weight * historical_share
        )

    other_stock = sum(
        remaining_stock[variant]
        for variant in other_variants
    )

    if other_stock > 0:
        # Unmapped sizes remain neutral. The factor never guesses their
        # historical behavior.
        group_weights["__OTHER__"] = (
            other_stock / physical_total
        )

    group_caps = {
        group: (
            other_stock
            if group == "__OTHER__"
            else stock_by_group.get(group, 0)
        )
        for group in group_weights
    }

    group_targets = allocate_integer_with_caps(
        remaining_total,
        group_weights,
        group_caps,
    )

    variant_targets = {
        variant: 0
        for variant in remaining_stock
    }

    for group, group_target in group_targets.items():
        if group == "__OTHER__":
            variants = other_variants
        else:
            variants = [
                variant
                for variant in remaining_stock
                if group_by_item.get(variant) == group
            ]

        caps = {
            variant: remaining_stock[variant]
            for variant in variants
        }
        weights = {
            variant: float(remaining_stock[variant])
            for variant in variants
        }

        split = allocate_integer_with_caps(
            group_target,
            weights,
            caps,
        )

        for variant, quantity in split.items():
            variant_targets[variant] = quantity

    return variant_targets


def _preference_weight(
    profile,
    store,
    group,
    weight,
):
    if not group:
        return 1.0

    index = max(
        0.0,
        flt(
            profile["indices"].get(
                (store, group),
                1.0,
            )
        ),
    )

    return max(
        0.000001,
        (1 - weight) + weight * index,
    )


def _scarcity_pressure(
    variant,
    variants,
    remaining_stock,
    group_by_item,
    network_group_shares,
):
    group = group_by_item.get(variant)
    if not group:
        return (0.0, variant)

    total_stock = sum(remaining_stock.values())
    if total_stock <= 0:
        return (0.0, variant)

    stock_group = sum(
        remaining_stock[value]
        for value in variants
        if group_by_item.get(value) == group
    )
    stock_share = stock_group / total_stock

    historical_share = max(
        0.0,
        flt(
            network_group_shares.get(
                group,
                0,
            )
        ),
    )

    ratio = (
        historical_share / stock_share
        if stock_share > 0
        else historical_share
    )
    return (ratio, variant)


def _allocate_variant_to_stores(
    variant,
    quantity,
    stores,
    quantities,
    remaining_need,
    profile,
    group_by_item,
    weight,
):
    quantity = max(0, int(quantity))
    if quantity <= 0:
        return 0

    group = group_by_item.get(variant)
    caps = {}
    weights = {}

    for store in stores:
        room = min(
            remaining_need[store.warehouse],
            _variant_room(
                store,
                quantities,
                variant,
            ),
        )
        if room <= 0:
            continue

        caps[store.warehouse] = room
        weights[store.warehouse] = _preference_weight(
            profile,
            store.warehouse,
            group,
            weight,
        )

    if not caps:
        return 0

    assigned = allocate_integer_with_caps(
        quantity,
        weights,
        caps,
    )

    total = 0
    for store_name, value in assigned.items():
        if value <= 0:
            continue

        quantities[store_name][variant] += value
        remaining_need[store_name] -= value
        total += value

    return total


def allocate_style_with_size_performance(
    variant_stock,
    target_total,
    stores,
    allowed_stores,
    profile,
    group_by_item,
    weight_percent,
):
    """Optimize size mix without changing the existing store total allocation.

    The current allocator remains the authoritative baseline for:
      - total Qty per store;
      - Tier minimum bundle;
      - Tier maximum per variant;
      - Related Set eligible stores;
      - physical stock feasibility.

    Size Performance is a secondary objective only.
    """
    stores = [
        store
        for store in stores
        if (
            allowed_stores is None
            or store.warehouse in allowed_stores
        )
    ]

    baseline = allocate_style(
        variant_stock,
        target_total,
        stores,
        allowed_stores=None,
    )

    weight = min(
        1.0,
        max(
            0.0,
            flt(weight_percent) / 100.0,
        ),
    )

    if (
        weight <= 0
        or not profile
        or not stores
    ):
        return baseline

    physical_stock = {
        variant: max(0, floor(quantity))
        for variant, quantity in variant_stock.items()
    }
    variants = list(physical_stock)

    target_store_totals = {
        store.warehouse: sum(
            baseline.quantities.get(
                store.warehouse,
                {},
            ).values()
        )
        for store in stores
    }

    fixed = _fixed_minimums(
        baseline,
        stores,
        variants,
    )

    quantities = {
        store.warehouse: {
            variant: fixed[
                store.warehouse
            ][variant]
            for variant in variants
        }
        for store in stores
    }

    remaining_need = {
        store.warehouse: max(
            0,
            target_store_totals[
                store.warehouse
            ]
            - sum(
                quantities[
                    store.warehouse
                ].values()
            ),
        )
        for store in stores
    }

    remaining_stock = {
        variant: max(
            0,
            physical_stock[variant]
            - sum(
                quantities[
                    store.warehouse
                ][variant]
                for store in stores
            ),
        )
        for variant in variants
    }

    remaining_total = sum(remaining_need.values())
    if remaining_total <= 0:
        return baseline

    desired_variant = _desired_variant_additions(
        remaining_stock,
        remaining_total,
        group_by_item,
        profile["network_group_shares"],
        weight,
    )

    ordered_variants = sorted(
        variants,
        key=lambda variant: _scarcity_pressure(
            variant,
            variants,
            remaining_stock,
            group_by_item,
            profile["network_group_shares"],
        ),
        reverse=True,
    )

    # Phase A: approach the blended historical/stock size mix.
    for variant in ordered_variants:
        target = min(
            remaining_stock[variant],
            max(
                0,
                int(
                    desired_variant.get(
                        variant,
                        0,
                    )
                ),
            ),
        )
        if target <= 0:
            continue

        assigned = _allocate_variant_to_stores(
            variant,
            target,
            stores,
            quantities,
            remaining_need,
            profile,
            group_by_item,
            weight,
        )

        remaining_stock[variant] -= assigned

    # Phase B: historical size mix is soft. If buying mix or Tier caps make
    # that target impossible, use remaining physical stock to preserve the
    # original total Qty assigned to every store.
    for variant in ordered_variants:
        available = min(
            remaining_stock[variant],
            sum(remaining_need.values()),
        )
        if available <= 0:
            continue

        assigned = _allocate_variant_to_stores(
            variant,
            available,
            stores,
            quantities,
            remaining_need,
            profile,
            group_by_item,
            weight,
        )

        remaining_stock[variant] -= assigned

    # Safety fallback: never return an incomplete/unsafe matrix.
    if any(
        quantity > 0
        for quantity in remaining_need.values()
    ):
        return baseline

    for variant in variants:
        allocated = sum(
            quantities[
                store.warehouse
            ][variant]
            for store in stores
        )
        if allocated > physical_stock[variant]:
            return baseline

    for store in stores:
        store_total = sum(
            quantities[store.warehouse].values()
        )

        if (
            store_total
            != target_store_totals[
                store.warehouse
            ]
        ):
            return baseline

        maximum = max(
            0,
            int(store.maximum_per_style or 0),
        )
        if maximum and any(
            quantities[
                store.warehouse
            ][variant] > maximum
            for variant in variants
        ):
            return baseline

    variant_targets = {
        variant: sum(
            quantities[
                store.warehouse
            ][variant]
            for store in stores
        )
        for variant in variants
    }

    depth_targets = {
        store.warehouse: max(
            0,
            target_store_totals[
                store.warehouse
            ]
            - sum(
                fixed[
                    store.warehouse
                ].values()
            ),
        )
        for store in stores
    }

    return StyleAllocation(
        quantities=quantities,
        variant_targets=variant_targets,
        unallocated={
            variant: 0
            for variant in variants
        },
        skipped_stores=list(
            baseline.skipped_stores
        ),
        depth_targets=depth_targets,
    )
