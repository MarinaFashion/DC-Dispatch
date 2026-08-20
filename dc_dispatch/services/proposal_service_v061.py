from __future__ import annotations

from collections import defaultdict
from math import floor
from time import perf_counter

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

import dc_dispatch.services.run_service as rs
from dc_dispatch.services import history_policy_service as history
from dc_dispatch.services import size_performance_service as size_perf


def _sales_from_breakdown(breakdown):
    result = {}
    for key, values in breakdown.items():
        demand_qty = flt(values.get("demand_qty"))
        if demand_qty != 0:
            result[key] = demand_qty
    return result


def _validate_history_from_breakdown(
    run,
    breakdown,
    candidate_templates,
    template_values,
):
    allowed_templates = history._allowed_templates_for_history_check(
        run,
        candidate_templates,
        template_values,
    )

    totals = defaultdict(float)
    for (template, store), values in breakdown.items():
        if template not in allowed_templates:
            continue
        totals[store] += max(
            0,
            flt(values.get("demand_qty")),
        )

    no_history = []
    for row in run.store_rules:
        demand = max(
            0,
            totals[row.store_warehouse],
        )

        if demand > 0:
            row.history_status = "Has History"
        else:
            row.history_status = "No History"

            if row.decision == "Include":
                no_history.append(
                    row.store_warehouse
                )
            elif (
                row.decision == "Use Reference Store"
                and max(
                    0,
                    totals[row.reference_store],
                )
                <= 0
            ):
                row.history_status = (
                    "Reference Store Missing"
                )
                no_history.append(
                    row.store_warehouse
                )

    return no_history


def _demand_breakdown_with_preloaded_returns(
    run,
    stores,
    resolved_returns,
):
    """Same policy as history.demand_sales_breakdown without resolving returns twice."""
    result = {}

    for row in history._gross_sales_rows(run, stores):
        key = (
            row.item_template,
            row.store_warehouse,
        )
        result[key] = {
            "gross_sales": flt(row.gross_sales),
            "same_store_returns": 0.0,
            "cross_store_returns_received": 0.0,
            "unlinked_returns_received": 0.0,
            "demand_qty": flt(row.gross_sales),
        }

    store_set = set(stores)

    for row in resolved_returns or []:
        return_store = row.return_store_warehouse

        if return_store not in store_set:
            continue

        key = (
            row.item_template,
            return_store,
        )
        bucket = result.setdefault(
            key,
            {
                "gross_sales": 0.0,
                "same_store_returns": 0.0,
                "cross_store_returns_received": 0.0,
                "unlinked_returns_received": 0.0,
                "demand_qty": 0.0,
            },
        )

        if (
            row.return_classification
            == "Same-Store Return - Deducted"
        ):
            bucket["same_store_returns"] += flt(
                row.return_qty
            )
        elif (
            row.return_classification
            == "Cross-Store Return - Excluded"
        ):
            bucket[
                "cross_store_returns_received"
            ] += flt(row.return_qty)
        else:
            bucket[
                "unlinked_returns_received"
            ] += flt(row.return_qty)

    for bucket in result.values():
        bucket["demand_qty"] = (
            flt(bucket["gross_sales"])
            - flt(
                bucket[
                    "same_store_returns"
                ]
            )
        )

    return result


@frappe.whitelist()
def calculate_proposal_optimized(run_name):
    """v0.6.1 proposal calculation with indexed Size Performance history."""
    started = perf_counter()
    timings = {}

    run = frappe.get_doc(
        "DC Dispatch Run",
        run_name,
    )

    rs._require_editable(run)
    rs._require_saved(run)

    if not run.items:
        frappe.throw(
            _("Load the target items before calculating.")
        )
    if not run.reference_fields:
        frappe.throw(
            _(
                "Select at least one historical "
                "matching field."
            )
        )
    if not run.store_rules:
        frappe.throw(
            _(
                "Load eligible stores before calculating."
            )
        )

    rs._settings_and_validate()
    size_perf.validate_size_factor_inputs(run)
    rs._validate_reference_fields(run)
    rs._lock_item_templates(
        [
            row.item_template
            for row in run.items
        ]
    )
    rs._validate_not_dispatched_elsewhere(
        run,
        [
            row.item_template
            for row in run.items
        ],
    )

    settings = frappe.get_single(
        "DC Dispatch Settings"
    )
    rs._validate_related_set_members(
        run,
        settings,
    )

    included_stores = [
        row.store_warehouse
        for row in run.store_rules
        if row.decision != "Exclude"
    ]

    if not included_stores:
        frappe.throw(
            _(
                "At least one eligible store is required."
            )
        )

    size_enabled = bool(
        cint(
            getattr(
                run,
                "include_size_performance_factor",
                0,
            )
        )
    )

    # Main v0.6.1 performance change:
    # When Size Performance is enabled, resolve returns once and reuse the
    # same rows for both the normal demand calculation and size history.
    history_started = perf_counter()

    if size_enabled:
        resolved_returns = history._return_rows(
            run,
            included_stores,
        )
        breakdown = (
            _demand_breakdown_with_preloaded_returns(
                run,
                included_stores,
                resolved_returns,
            )
        )
    else:
        resolved_returns = None
        breakdown = history.demand_sales_breakdown(
            run,
            included_stores,
        )

    timings["historical_demand_seconds"] = round(
        perf_counter() - history_started,
        3,
    )

    sales = _sales_from_breakdown(
        breakdown
    )

    candidate_templates = {
        template
        for template, _store in sales
    }
    target_templates = {
        row.item_template
        for row in run.items
    }

    value_fields = (
        rs._reference_fieldnames(run)
        | history.historical_filter_fieldnames(run)
        | {
            settings.item_main_group_field,
            settings.item_subgroup_field,
            settings.item_related_set_field,
        }
    )

    template_values = rs._item_values(
        candidate_templates
        | target_templates,
        value_fields,
    )

    no_history = _validate_history_from_breakdown(
        run,
        breakdown,
        candidate_templates,
        template_values,
    )

    if no_history:
        frappe.throw(
            _(
                "Resolve stores without history by choosing Exclude or "
                "Use Reference Store: {0}"
            ).format(
                ", ".join(no_history)
            )
        )

    stock_by_template = rs.get_variant_stock_bulk(
        [
            row.item_template
            for row in run.items
        ],
        run.source_warehouse,
    )
    fields_by_group = (
        rs._fields_by_main_group(run)
    )

    prepared = {}
    related_members = defaultdict(list)

    preparation_started = perf_counter()

    # First prepare all normal cohorts. Do NOT build Size Performance
    # history yet. This allows the later size SQL query to be restricted
    # to only the union of cohorts the run actually uses.
    for item_row in run.items:
        stock = stock_by_template.get(
            item_row.item_template,
            {},
        )
        current_total = sum(
            stock.values()
        )

        if (
            current_total
            != floor(
                flt(item_row.dc_qty)
            )
        ):
            item_row.dc_qty = current_total

        target_total = min(
            current_total,
            rs._round_whole(
                current_total
                * flt(
                    item_row.dispatch_percentage
                )
                / 100
            ),
        )
        item_row.target_qty = (
            target_total
        )

        scoped_templates = (
            history.historical_scope_candidates(
                run,
                item_row,
                candidate_templates,
                template_values,
            )
        )

        scoped_sales = {
            key: qty
            for key, qty in sales.items()
            if key[0] in scoped_templates
        }

        scores, evidence, cohort = (
            rs._cohort_scores(
                item_row,
                template_values,
                scoped_sales,
                fields_by_group,
                settings.item_main_group_field,
                flt(
                    run.minimum_match_percent
                ),
            )
        )

        adjusted_scores, missing_references = (
            rs._adjust_store_scores(
                run,
                scores,
            )
        )

        warning = rs._evidence_warning(
            evidence,
            settings,
        )

        selected_fields = (
            fields_by_group.get(
                item_row.main_group,
                [],
            )
        )

        missing_target_fields = [
            field
            for field in selected_fields
            if not rs._has_value(
                template_values[
                    item_row.item_template
                ].get(field)
            )
        ]

        if missing_target_fields:
            warning = "; ".join(
                value
                for value in [
                    warning,
                    "Target item is blank for: "
                    + ", ".join(
                        missing_target_fields
                    ),
                ]
                if value
            )

        if missing_references:
            reference_warning = (
                "Reference store has no demand for this cohort: "
                + ", ".join(
                    missing_references
                )
            )
            warning = "; ".join(
                value
                for value in [
                    warning,
                    reference_warning,
                ]
                if value
            )

        if not scoped_templates:
            scope_warning = (
                "No historical templates passed "
                "the Historical Reference Filters"
            )
            warning = "; ".join(
                value
                for value in [
                    warning,
                    scope_warning,
                ]
                if value
            )

        item_row.cohort_templates = evidence[
            "templates"
        ]
        item_row.cohort_units = evidence[
            "units"
        ]
        item_row.cohort_stores = evidence[
            "stores"
        ]
        item_row.warning = warning

        prepared[
            item_row.item_template
        ] = {
            "row": item_row,
            "stock": stock,
            "target": target_total,
            "scores": adjusted_scores,
            "cohort": cohort,
            "warning": warning,
            "size_profile": None,
        }

        if item_row.related_set:
            related_members[
                item_row.related_set
            ].append(
                item_row.item_template
            )

    timings[
        "cohort_preparation_seconds"
    ] = round(
        perf_counter()
        - preparation_started,
        3,
    )

    size_context = None

    if size_enabled:
        size_started = perf_counter()

        size_history_templates = set()
        for value in prepared.values():
            size_history_templates.update(
                value["cohort"] or []
            )

        target_item_codes = {
            item_code
            for stock in stock_by_template.values()
            for item_code in stock
        }

        size_context = (
            size_perf.build_size_context(
                run,
                included_stores,
                target_item_codes,
                size_history_templates,
                resolved_returns,
            )
        )

        for prepared_item in prepared.values():
            profile = (
                size_perf.profile_for_cohort(
                    run,
                    prepared_item["cohort"],
                    size_context,
                )
            )
            prepared_item[
                "size_profile"
            ] = profile

            if not profile:
                warning = "; ".join(
                    value
                    for value in [
                        prepared_item[
                            "warning"
                        ],
                        (
                            "Size Performance Factor skipped: "
                            "no mapped size sales were found "
                            "in this historical cohort."
                        ),
                    ]
                    if value
                )
                prepared_item[
                    "warning"
                ] = warning
                prepared_item[
                    "row"
                ].warning = warning

        timings[
            "size_history_seconds"
        ] = round(
            perf_counter()
            - size_started,
            3,
        )
    else:
        timings[
            "size_history_seconds"
        ] = 0

    store_rules = {
        row.store_warehouse: row
        for row in run.store_rules
        if row.decision != "Exclude"
    }

    allowed_by_template = {
        template: None
        for template in prepared
    }
    set_scores = {}

    for related_set, members in (
        related_members.items()
    ):
        combined_scores = defaultdict(float)
        member_stock = {}
        member_targets = {}

        for template in members:
            prepared_item = prepared[
                template
            ]
            score_total = sum(
                prepared_item[
                    "scores"
                ].values()
            )

            for store, score in (
                prepared_item[
                    "scores"
                ].items()
            ):
                combined_scores[
                    store
                ] += (
                    score / score_total
                    if score_total
                    else 0
                )

            member_stock[
                template
            ] = prepared_item["stock"]
            member_targets[
                template
            ] = prepared_item["target"]

        set_store_inputs = rs._store_inputs(
            store_rules,
            combined_scores,
        )

        allowed = rs.choose_related_set_stores(
            member_stock,
            member_targets,
            set_store_inputs,
        )

        if not allowed:
            frappe.throw(
                _(
                    "Related Set {0} cannot cover a complete "
                    "size bundle for any store."
                ).format(
                    related_set
                )
            )

        set_scores[
            related_set
        ] = dict(combined_scores)

        for template in members:
            allowed_by_template[
                template
            ] = allowed

    next_revision = (
        int(run.revision or 0) + 1
    )

    proposal_values = []
    snapshot_values = []
    total_suggested = 0

    allocation_started = perf_counter()

    for template, prepared_item in (
        prepared.items()
    ):
        item_row = prepared_item[
            "row"
        ]

        allocation_scores = (
            set_scores[
                item_row.related_set
            ]
            if item_row.related_set
            else prepared_item["scores"]
        )

        store_inputs = rs._store_inputs(
            store_rules,
            allocation_scores,
        )

        if (
            size_context
            and prepared_item.get(
                "size_profile"
            )
        ):
            allocation = (
                size_perf.allocate_style_with_size_performance(
                    prepared_item["stock"],
                    prepared_item["target"],
                    store_inputs,
                    allowed_by_template[
                        template
                    ],
                    prepared_item[
                        "size_profile"
                    ],
                    size_context[
                        "group_by_item"
                    ],
                    run.size_performance_weight,
                )
            )
        else:
            allocation = rs.allocate_style(
                prepared_item["stock"],
                prepared_item["target"],
                store_inputs,
                allowed_stores=(
                    allowed_by_template[
                        template
                    ]
                ),
            )

        score_total = sum(
            max(0, value)
            for value in allocation_scores.values()
        )

        for warehouse, rule in (
            store_rules.items()
        ):
            for item_code in (
                allocation.variant_targets
            ):
                suggested = (
                    allocation.quantities.get(
                        warehouse,
                        {},
                    ).get(
                        item_code,
                        0,
                    )
                )
                total_suggested += (
                    suggested
                )

                proposal_values.append(
                    {
                        "run": run.name,
                        "revision": next_revision,
                        "source_warehouse": (
                            run.source_warehouse
                        ),
                        "store_warehouse": warehouse,
                        "transit_warehouse": (
                            rule.transit_warehouse
                        ),
                        "item_template": template,
                        "item_code": item_code,
                        "main_group": (
                            item_row.main_group
                        ),
                        "related_set": (
                            item_row.related_set
                        ),
                        "sales_score": (
                            allocation_scores.get(
                                warehouse,
                                0,
                            )
                        ),
                        "share_percent": (
                            allocation_scores.get(
                                warehouse,
                                0,
                            )
                            * 100
                            / score_total
                            if score_total
                            else 0
                        ),
                        "suggested_qty": suggested,
                        "final_qty": suggested,
                        "exclude": 0,
                        "validation_status": (
                            "Warning"
                            if prepared_item[
                                "warning"
                            ]
                            else "Valid"
                        ),
                    }
                )

        for item_code, quantity in (
            prepared_item[
                "stock"
            ].items()
        ):
            snapshot_values.append(
                {
                    "run": run.name,
                    "revision": next_revision,
                    "warehouse": (
                        run.source_warehouse
                    ),
                    "item_code": item_code,
                    "actual_qty": quantity,
                }
            )

    timings[
        "allocation_seconds"
    ] = round(
        perf_counter()
        - allocation_started,
        3,
    )

    persistence_started = perf_counter()

    frappe.db.delete(
        "DC Dispatch Proposal Line",
        {"run": run.name},
    )
    frappe.db.delete(
        "DC Dispatch Stock Snapshot",
        {"run": run.name},
    )

    rs._bulk_insert(
        "DC Dispatch Proposal Line",
        proposal_values,
    )
    rs._bulk_insert(
        "DC Dispatch Stock Snapshot",
        snapshot_values,
    )

    run.revision = next_revision
    run.calculated_at = now_datetime()
    run.stock_snapshot_hash = (
        rs._snapshot_hash(
            snapshot_values
        )
    )
    run.calculation_input_hash = (
        rs._calculation_input_hash(run)
    )
    run.size_performance_signature = (
        size_perf.configuration_signature(
            run
        )
    )
    run.proposal_file = None
    run.status = "Calculated"
    run.save()

    rs.validate_current_proposal(run)

    timings[
        "persistence_seconds"
    ] = round(
        perf_counter()
        - persistence_started,
        3,
    )
    timings[
        "total_seconds"
    ] = round(
        perf_counter() - started,
        3,
    )

    return {
        "revision": next_revision,
        "styles": len(prepared),
        "lines": len(proposal_values),
        "suggested_qty": total_suggested,
        "warnings": sum(
            1
            for value in prepared.values()
            if value["warning"]
        ),
        "history_scans": 1,
        "size_performance_enabled": (
            size_enabled
        ),
        "size_performance_weight": flt(
            getattr(
                run,
                "size_performance_weight",
                0,
            )
        ),
        "size_history_rows": (
            size_context.get(
                "gross_rows",
                0,
            )
            if size_context
            else 0
        ),
        "size_history_templates": (
            size_context.get(
                "aggregated_templates",
                0,
            )
            if size_context
            else 0
        ),
        "timings": timings,
    }
