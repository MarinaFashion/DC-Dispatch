from __future__ import annotations

from collections import defaultdict
from math import floor

import frappe
from frappe.utils import cint, flt

from dc_dispatch.services import historical_cache_service as history_cache
from dc_dispatch.services import history_evidence_service as evidence_base
from dc_dispatch.services import run_service as rs
from dc_dispatch.services import size_performance_service as size_perf


SHEET_NAME = "Size Performance Reconciliation"


def _header_map(sheet):
    return {
        str(cell.value or "").strip(): cell.column
        for cell in sheet[1]
        if cell.value
    }


def _cohorts_from_workbook(workbook):
    sheet = workbook["Historical Templates"]
    headers = _header_map(sheet)

    target_col = headers.get("Target Item Template")
    historical_col = headers.get("Historical Item Template Used")

    if not target_col or not historical_col:
        frappe.throw(
            "Historical Evidence is missing the Historical Templates columns "
            "required for Size Performance reconciliation."
        )

    cohorts = defaultdict(set)

    for row_number in range(2, sheet.max_row + 1):
        target = sheet.cell(row_number, target_col).value
        historical = sheet.cell(row_number, historical_col).value

        if target and historical:
            cohorts[str(target)].add(str(historical))

    return {
        target: sorted(values)
        for target, values in cohorts.items()
    }


def _proposal_rows(run):
    if not run.revision:
        return []

    return frappe.get_all(
        "DC Dispatch Proposal Line",
        filters={
            "run": run.name,
            "revision": run.revision,
        },
        fields=[
            "item_template",
            "item_code",
            "store_warehouse",
            "sales_score",
            "suggested_qty",
            "final_qty",
            "exclude",
        ],
        order_by=(
            "item_template asc, "
            "store_warehouse asc, "
            "item_code asc"
        ),
        limit_page_length=0,
    )


def _proposal_indexes(rows):
    scores = {}
    suggested = {}
    final = {}

    for row in rows:
        score_key = (
            row.item_template,
            row.store_warehouse,
        )
        if score_key not in scores:
            scores[score_key] = flt(row.sales_score)

        qty_key = (
            row.item_template,
            row.store_warehouse,
            row.item_code,
        )
        suggested[qty_key] = cint(row.suggested_qty)
        final[qty_key] = cint(row.final_qty)

    return scores, suggested, final


def _stock_snapshot_by_template(run):
    rows = frappe.get_all(
        "DC Dispatch Stock Snapshot",
        filters={
            "run": run.name,
            "revision": run.revision,
        },
        fields=[
            "item_code",
            "actual_qty",
        ],
        order_by="item_code asc",
        limit_page_length=0,
    )

    if not rows:
        frappe.throw(
            "No DC Dispatch Stock Snapshot exists for this proposal revision."
        )

    item_codes = [
        row.item_code
        for row in rows
        if row.item_code
    ]

    item_rows = frappe.get_all(
        "Item",
        filters={"name": ["in", item_codes]},
        fields=["name", "variant_of"],
        limit_page_length=0,
    )
    template_by_item = {
        row.name: (
            row.variant_of
            if row.variant_of
            else row.name
        )
        for row in item_rows
    }

    result = defaultdict(dict)

    for row in rows:
        template = template_by_item.get(
            row.item_code,
            row.item_code,
        )
        result[template][row.item_code] = max(
            0,
            floor(flt(row.actual_qty)),
        )

    return dict(result)


def _scores_by_template(run, proposal_scores):
    result = {}

    for item_row in run.items:
        result[item_row.item_template] = {
            rule.store_warehouse: max(
                0.0,
                flt(
                    proposal_scores.get(
                        (
                            item_row.item_template,
                            rule.store_warehouse,
                        ),
                        0,
                    )
                ),
            )
            for rule in run.store_rules
            if rule.decision != "Exclude"
        }

    return result


def _allowed_stores_by_template(
    run,
    stock_by_template,
    target_by_template,
    scores_by_template,
    store_rules,
):
    result = {
        row.item_template: None
        for row in run.items
    }

    related_members = defaultdict(list)
    for row in run.items:
        related_set = str(
            row.related_set or ""
        ).strip()
        if related_set:
            related_members[related_set].append(
                row.item_template
            )

    for related_set, members in related_members.items():
        if not members:
            continue

        # Proposal lines of all members in a Related Set store the same
        # combined Related Set score. Use one member as the exact score
        # basis that was persisted by the calculation.
        combined_scores = dict(
            scores_by_template.get(
                members[0],
                {},
            )
        )

        inputs = rs._store_inputs(
            store_rules,
            combined_scores,
        )

        allowed = rs.choose_related_set_stores(
            {
                template: stock_by_template.get(
                    template,
                    {},
                )
                for template in members
            },
            {
                template: target_by_template.get(
                    template,
                    0,
                )
                for template in members
            },
            inputs,
        )

        for template in members:
            result[template] = allowed

    return result


def _replay_allocations(
    run,
    cohorts,
    proposal_scores,
    stock_by_template,
):
    active_rules = {
        row.store_warehouse: row
        for row in run.store_rules
        if row.decision != "Exclude"
    }
    stores = list(active_rules)

    target_by_template = {
        row.item_template: max(
            0,
            cint(row.target_qty),
        )
        for row in run.items
    }

    scores_by_template = _scores_by_template(
        run,
        proposal_scores,
    )

    allowed_by_template = (
        _allowed_stores_by_template(
            run,
            stock_by_template,
            target_by_template,
            scores_by_template,
            active_rules,
        )
    )

    target_item_codes = {
        item_code
        for stock in stock_by_template.values()
        for item_code in stock
    }

    cache_data = history_cache.load_cache_data(
        run,
        require_valid=True,
    )

    size_context = (
        size_perf.build_size_context_from_cache(
            run,
            stores,
            target_item_codes,
            cache_data["size_by_template"],
            cache_data.get(
                "source_row_count",
                0,
            ),
        )
    )

    replay = {}

    for item_row in run.items:
        template = item_row.item_template
        stock = stock_by_template.get(
            template,
            {},
        )
        target = target_by_template.get(
            template,
            0,
        )
        scores = scores_by_template.get(
            template,
            {},
        )
        inputs = rs._store_inputs(
            active_rules,
            scores,
        )
        allowed = allowed_by_template.get(
            template
        )

        baseline = rs.allocate_style(
            stock,
            target,
            inputs,
            allowed_stores=allowed,
        )

        profile = size_perf.profile_for_cohort(
            run,
            cohorts.get(template, []),
            size_context,
        )

        if (
            cint(
                getattr(
                    run,
                    "include_size_performance_factor",
                    0,
                )
            )
            and profile
        ):
            expected = (
                size_perf.allocate_style_with_size_performance(
                    stock,
                    target,
                    inputs,
                    allowed,
                    profile,
                    size_context["group_by_item"],
                    run.size_performance_weight,
                )
            )
            factor_applied = True
        else:
            expected = baseline
            factor_applied = False

        replay[template] = {
            "baseline": baseline,
            "expected": expected,
            "profile": profile,
            "factor_applied": factor_applied,
            "group_by_item": size_context[
                "group_by_item"
            ],
            "cohort_count": len(
                cohorts.get(template, [])
            ),
        }

    return replay


def _preference_multiplier(
    profile,
    store,
    group,
    weight,
):
    if not profile or not group:
        return 1.0, 1.0

    index = max(
        0.0,
        flt(
            profile["indices"].get(
                (store, group),
                1.0,
            )
        ),
    )
    multiplier = max(
        0.000001,
        (1.0 - weight)
        + weight * index,
    )
    return index, multiplier


def _write_size_reconciliation(
    workbook,
    run,
    cohorts,
    replay,
    suggested,
    final,
):
    if SHEET_NAME in workbook.sheetnames:
        workbook.remove(
            workbook[SHEET_NAME]
        )

    score_index = workbook.sheetnames.index(
        "Score Reconciliation"
    )
    sheet = workbook.create_sheet(
        SHEET_NAME,
        score_index + 1,
    )

    headers = [
        "Target Item Template",
        "Store Warehouse",
        "Item Variant",
        "Size Group",
        "Size Factor Applied",
        "Size Performance Weight %",
        "Historical Cohort Templates",
        "Mapped Cohort Size Units",
        "Network Size-Group Share %",
        "Store Relative Size Performance Index",
        "Blended Store Preference Multiplier",
        "Baseline Qty Without Size Factor",
        "Expected Qty With Size Factor",
        "Stored Suggested Qty",
        "Current Final Qty",
        "Reconciles to Calculated Proposal",
        "Note",
    ]
    sheet.append(headers)

    weight_percent = flt(
        getattr(
            run,
            "size_performance_weight",
            0,
        )
    )
    weight = min(
        1.0,
        max(
            0.0,
            weight_percent / 100.0,
        ),
    )

    for item_row in run.items:
        template = item_row.item_template
        data = replay.get(template)
        if not data:
            continue

        baseline = data["baseline"]
        expected = data["expected"]
        profile = data["profile"]
        group_by_item = data["group_by_item"]

        variants = sorted(
            expected.variant_targets
        )

        for rule in run.store_rules:
            if rule.decision == "Exclude":
                continue

            store = rule.store_warehouse

            for item_code in variants:
                group = group_by_item.get(
                    item_code
                )
                index, multiplier = (
                    _preference_multiplier(
                        profile,
                        store,
                        group,
                        weight,
                    )
                )

                network_share = (
                    flt(
                        profile[
                            "network_group_shares"
                        ].get(
                            group,
                            0,
                        )
                    )
                    * 100.0
                    if profile and group
                    else 0.0
                )

                baseline_qty = cint(
                    baseline.quantities.get(
                        store,
                        {},
                    ).get(
                        item_code,
                        0,
                    )
                )
                expected_qty = cint(
                    expected.quantities.get(
                        store,
                        {},
                    ).get(
                        item_code,
                        0,
                    )
                )
                suggested_qty = cint(
                    suggested.get(
                        (
                            template,
                            store,
                            item_code,
                        ),
                        0,
                    )
                )
                final_qty = cint(
                    final.get(
                        (
                            template,
                            store,
                            item_code,
                        ),
                        0,
                    )
                )

                reconciles = (
                    "Yes"
                    if expected_qty
                    == suggested_qty
                    else "No"
                )

                if not data[
                    "factor_applied"
                ]:
                    note = (
                        "Size Performance not applied "
                        "for this target; baseline used."
                    )
                elif not group:
                    note = (
                        "Unmapped size; neutral "
                        "Size Performance multiplier."
                    )
                elif final_qty != suggested_qty:
                    note = (
                        "Current Final Qty differs from "
                        "the calculated Suggested Qty "
                        "(manual proposal edit/import)."
                    )
                else:
                    note = ""

                sheet.append(
                    [
                        template,
                        store,
                        item_code,
                        group or "Unmapped",
                        (
                            "Yes"
                            if data[
                                "factor_applied"
                            ]
                            else "No"
                        ),
                        weight_percent,
                        data["cohort_count"],
                        (
                            flt(
                                profile[
                                    "mapped_units"
                                ]
                            )
                            if profile
                            else 0
                        ),
                        network_share,
                        index,
                        multiplier,
                        baseline_qty,
                        expected_qty,
                        suggested_qty,
                        final_qty,
                        reconciles,
                        note,
                    ]
                )

    evidence_base._format_sheet(
        sheet,
        freeze="A2",
        auto_filter=True,
    )



def add_size_performance_reconciliation(workbook, run):
    """Add an exact read-only replay of the Size Performance calculation."""
    size_perf.assert_size_configuration_unchanged(run)

    cohorts = _cohorts_from_workbook(workbook)
    rows = _proposal_rows(run)
    proposal_scores, suggested, final = _proposal_indexes(rows)
    stock_by_template = _stock_snapshot_by_template(run)

    replay = _replay_allocations(
        run,
        cohorts,
        proposal_scores,
        stock_by_template,
    )

    _write_size_reconciliation(
        workbook,
        run,
        cohorts,
        replay,
        suggested,
        final,
    )
