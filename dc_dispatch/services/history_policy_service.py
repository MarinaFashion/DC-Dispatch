from __future__ import annotations

from collections import defaultdict
from math import floor

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

import dc_dispatch.services.run_service as rs


def demand_sales_breakdown(run, stores: list[str]):
    """Return sales-history components by Item Template and physical store.

    Demand policy:
        Demand Units = Gross Sales - Same-Store Returns

    Cross-store returns are deliberately excluded from demand because the
    merchandise physically remains at another store. Unlinked returns are
    also excluded because their originating store cannot be proven.
    """
    if not stores:
        return {}

    has_original_link = bool(
        frappe.get_meta("Sales Invoice Item").get_field("sales_invoice_item")
    )

    if has_original_link:
        original_join = (
            "LEFT JOIN `tabSales Invoice Item` original_item "
            "ON original_item.name = sii.sales_invoice_item"
        )
        same_store_return = """
            CASE
                WHEN si.is_return = 1
                 AND original_item.name IS NOT NULL
                 AND original_item.warehouse = sii.warehouse
                THEN ABS(sii.qty)
                ELSE 0
            END
        """
        cross_store_return = """
            CASE
                WHEN si.is_return = 1
                 AND original_item.name IS NOT NULL
                 AND COALESCE(original_item.warehouse, '') != COALESCE(sii.warehouse, '')
                THEN ABS(sii.qty)
                ELSE 0
            END
        """
        unlinked_return = """
            CASE
                WHEN si.is_return = 1
                 AND original_item.name IS NULL
                THEN ABS(sii.qty)
                ELSE 0
            END
        """
    else:
        original_join = ""
        same_store_return = "0"
        cross_store_return = "0"
        unlinked_return = """
            CASE
                WHEN si.is_return = 1
                THEN ABS(sii.qty)
                ELSE 0
            END
        """

    rows = frappe.db.sql(
        f"""
        SELECT
            COALESCE(NULLIF(item.variant_of, ''), item.name) AS item_template,
            sii.warehouse AS store_warehouse,
            SUM(
                CASE
                    WHEN si.is_return = 0 THEN sii.qty
                    ELSE 0
                END
            ) AS gross_sales,
            SUM({same_store_return}) AS same_store_returns,
            SUM({cross_store_return}) AS cross_store_returns_received,
            SUM({unlinked_return}) AS unlinked_returns_received
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si
            ON si.name = sii.parent AND si.docstatus = 1
        INNER JOIN `tabItem` item
            ON item.name = sii.item_code
        {original_join}
        WHERE si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND sii.warehouse IN %(stores)s
        GROUP BY
            COALESCE(NULLIF(item.variant_of, ''), item.name),
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

    result = {}
    for row in rows:
        gross = flt(row.gross_sales)
        same_store_returns = flt(row.same_store_returns)
        cross_store_returns = flt(row.cross_store_returns_received)
        unlinked_returns = flt(row.unlinked_returns_received)
        result[(row.item_template, row.store_warehouse)] = {
            "gross_sales": gross,
            "same_store_returns": same_store_returns,
            "cross_store_returns_received": cross_store_returns,
            "unlinked_returns_received": unlinked_returns,
            "demand_qty": gross - same_store_returns,
        }
    return result


def demand_historical_sales(run, stores: list[str]):
    """Return the demand quantity map consumed by cohort scoring."""
    breakdown = demand_sales_breakdown(run, stores)
    return {
        key: flt(values["demand_qty"])
        for key, values in breakdown.items()
        if flt(values["demand_qty"]) != 0
    }


def return_audit_rows(run, stores: list[str]):
    """Return line-level return records for the evidence workbook."""
    if not stores:
        return []

    has_original_link = bool(
        frappe.get_meta("Sales Invoice Item").get_field("sales_invoice_item")
    )

    if has_original_link:
        original_join = (
            "LEFT JOIN `tabSales Invoice Item` original_item "
            "ON original_item.name = sii.sales_invoice_item"
        )
        fields = """
            original_item.parent AS original_sales_invoice,
            original_item.warehouse AS original_store_warehouse,
            CASE
                WHEN original_item.name IS NULL
                    THEN 'Unlinked Return - Excluded'
                WHEN original_item.warehouse = sii.warehouse
                    THEN 'Same-Store Return - Deducted'
                ELSE 'Cross-Store Return - Excluded'
            END AS return_classification
        """
        scope_expression = """
            (
                sii.warehouse IN %(stores)s
                OR original_item.warehouse IN %(stores)s
            )
        """
    else:
        original_join = ""
        fields = """
            NULL AS original_sales_invoice,
            NULL AS original_store_warehouse,
            'Unlinked Return - Excluded' AS return_classification
        """
        scope_expression = "sii.warehouse IN %(stores)s"

    return frappe.db.sql(
        f"""
        SELECT
            si.name AS return_sales_invoice,
            si.posting_date,
            COALESCE(NULLIF(item.variant_of, ''), item.name) AS item_template,
            sii.item_code,
            sii.warehouse AS return_store_warehouse,
            ABS(sii.qty) AS return_qty,
            {fields}
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si
            ON si.name = sii.parent AND si.docstatus = 1
        INNER JOIN `tabItem` item
            ON item.name = sii.item_code
        {original_join}
        WHERE si.company = %(company)s
          AND si.is_return = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND {scope_expression}
        ORDER BY si.posting_date, si.name, sii.idx
        """,
        {
            "company": run.company,
            "from_date": run.sales_from_date,
            "to_date": run.sales_to_date,
            "stores": tuple(stores),
        },
        as_dict=True,
    )


def analyze_store_history(run):
    """Store-history check using the demand-history policy."""
    rs._require_saved(run)
    if not run.store_rules:
        frappe.throw(_("Load eligible stores first."))

    stores = [row.store_warehouse for row in run.store_rules]
    sales = demand_historical_sales(run, stores)
    totals = defaultdict(float)
    for (_template, store), quantity in sales.items():
        totals[store] += quantity

    no_history = []
    for row in run.store_rules:
        if max(0, totals[row.store_warehouse]) > 0:
            row.history_status = "Has History"
        else:
            row.history_status = "No History"
            if row.decision == "Include":
                no_history.append(row.store_warehouse)
            elif (
                row.decision == "Use Reference Store"
                and max(0, totals[row.reference_store]) <= 0
            ):
                row.history_status = "Reference Store Missing"
                no_history.append(row.store_warehouse)

    run.status = (
        "Reference Review Required"
        if no_history
        else (run.status if run.status != "Draft" else "Items Loaded")
    )
    run.save()

    return {
        "no_history": no_history,
        "stores": [
            {
                "store": row.store_warehouse,
                # Keep net_units for backward compatibility with current UI/debug calls.
                "net_units": max(0, totals[row.store_warehouse]),
                "demand_units": max(0, totals[row.store_warehouse]),
                "status": row.history_status,
                "decision": row.decision,
                "reference_store": row.reference_store,
            }
            for row in run.store_rules
        ],
    }


def calculate_proposal(run):
    """Calculate proposal using Gross Sales - Same-Store Returns demand."""
    rs._require_editable(run)
    rs._require_saved(run)

    if not run.items:
        frappe.throw(_("Load the target items before calculating."))
    if not run.reference_fields:
        frappe.throw(_("Select at least one historical matching field."))
    if not run.store_rules:
        frappe.throw(_("Load eligible stores before calculating."))

    rs._settings_and_validate()
    rs._validate_reference_fields(run)
    rs._lock_item_templates([row.item_template for row in run.items])
    rs._validate_not_dispatched_elsewhere(
        run, [row.item_template for row in run.items]
    )

    history = analyze_store_history(run)
    if history["no_history"]:
        frappe.throw(
            _(
                "Resolve stores without history by choosing Exclude or "
                "Use Reference Store: {0}"
            ).format(", ".join(history["no_history"]))
        )

    settings = frappe.get_single("DC Dispatch Settings")
    rs._validate_related_set_members(run, settings)

    stock_by_template = rs.get_variant_stock_bulk(
        [row.item_template for row in run.items],
        run.source_warehouse,
    )
    sales = demand_historical_sales(
        run,
        [
            row.store_warehouse
            for row in run.store_rules
            if row.decision != "Exclude"
        ],
    )

    candidate_templates = {template for template, _store in sales}
    target_templates = {row.item_template for row in run.items}
    value_fields = rs._reference_fieldnames(run) | {
        settings.item_main_group_field,
        settings.item_subgroup_field,
        settings.item_related_set_field,
    }
    template_values = rs._item_values(
        candidate_templates | target_templates,
        value_fields,
    )
    fields_by_group = rs._fields_by_main_group(run)

    prepared = {}
    related_members = defaultdict(list)

    for item_row in run.items:
        stock = stock_by_template.get(item_row.item_template, {})
        current_total = sum(stock.values())

        if current_total != floor(flt(item_row.dc_qty)):
            item_row.dc_qty = current_total

        target_total = min(
            current_total,
            rs._round_whole(
                current_total * flt(item_row.dispatch_percentage) / 100
            ),
        )
        item_row.target_qty = target_total

        scores, evidence, cohort = rs._cohort_scores(
            item_row,
            template_values,
            sales,
            fields_by_group,
            settings.item_main_group_field,
            flt(run.minimum_match_percent),
        )
        adjusted_scores, missing_references = rs._adjust_store_scores(
            run, scores
        )

        warning = rs._evidence_warning(evidence, settings)
        selected_fields = fields_by_group.get(item_row.main_group, [])
        missing_target_fields = [
            field
            for field in selected_fields
            if not rs._has_value(
                template_values[item_row.item_template].get(field)
            )
        ]
        if missing_target_fields:
            warning = "; ".join(
                value
                for value in [
                    warning,
                    "Target item is blank for: "
                    + ", ".join(missing_target_fields),
                ]
                if value
            )

        if missing_references:
            reference_warning = (
                "Reference store has no demand for this cohort: "
                + ", ".join(missing_references)
            )
            warning = "; ".join(
                value for value in [warning, reference_warning] if value
            )

        item_row.cohort_templates = evidence["templates"]
        item_row.cohort_units = evidence["units"]
        item_row.cohort_stores = evidence["stores"]
        item_row.warning = warning

        prepared[item_row.item_template] = {
            "row": item_row,
            "stock": stock,
            "target": target_total,
            "scores": adjusted_scores,
            "cohort": cohort,
            "warning": warning,
        }

        if item_row.related_set:
            related_members[item_row.related_set].append(
                item_row.item_template
            )

    store_rules = {
        row.store_warehouse: row
        for row in run.store_rules
        if row.decision != "Exclude"
    }
    allowed_by_template: dict[str, set[str] | None] = {
        template: None for template in prepared
    }
    set_scores: dict[str, dict[str, float]] = {}

    for related_set, members in related_members.items():
        combined_scores = defaultdict(float)
        member_stock = {}
        member_targets = {}

        for template in members:
            prepared_item = prepared[template]
            score_total = sum(prepared_item["scores"].values())

            for store, score in prepared_item["scores"].items():
                combined_scores[store] += (
                    score / score_total if score_total else 0
                )

            member_stock[template] = prepared_item["stock"]
            member_targets[template] = prepared_item["target"]

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
                    "Related Set {0} cannot cover a complete size bundle "
                    "for any store."
                ).format(related_set)
            )

        set_scores[related_set] = dict(combined_scores)
        for template in members:
            allowed_by_template[template] = allowed

    next_revision = int(run.revision or 0) + 1
    frappe.db.delete("DC Dispatch Proposal Line", {"run": run.name})
    frappe.db.delete("DC Dispatch Stock Snapshot", {"run": run.name})

    proposal_values = []
    snapshot_values = []
    total_suggested = 0

    for template, prepared_item in prepared.items():
        item_row = prepared_item["row"]
        allocation_scores = (
            set_scores[item_row.related_set]
            if item_row.related_set
            else prepared_item["scores"]
        )
        store_inputs = rs._store_inputs(
            store_rules,
            allocation_scores,
        )
        allocation = rs.allocate_style(
            prepared_item["stock"],
            prepared_item["target"],
            store_inputs,
            allowed_stores=allowed_by_template[template],
        )

        score_total = sum(
            max(0, value) for value in allocation_scores.values()
        )

        for warehouse, rule in store_rules.items():
            for item_code in allocation.variant_targets:
                suggested = allocation.quantities.get(
                    warehouse, {}
                ).get(item_code, 0)
                total_suggested += suggested

                proposal_values.append(
                    {
                        "run": run.name,
                        "revision": next_revision,
                        "source_warehouse": run.source_warehouse,
                        "store_warehouse": warehouse,
                        "transit_warehouse": rule.transit_warehouse,
                        "item_template": template,
                        "item_code": item_code,
                        "main_group": item_row.main_group,
                        "related_set": item_row.related_set,
                        "sales_score": allocation_scores.get(
                            warehouse, 0
                        ),
                        "share_percent": (
                            allocation_scores.get(warehouse, 0)
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
                            if prepared_item["warning"]
                            else "Valid"
                        ),
                    }
                )

        for item_code, quantity in prepared_item["stock"].items():
            snapshot_values.append(
                {
                    "run": run.name,
                    "revision": next_revision,
                    "warehouse": run.source_warehouse,
                    "item_code": item_code,
                    "actual_qty": quantity,
                }
            )

    rs._bulk_insert("DC Dispatch Proposal Line", proposal_values)
    rs._bulk_insert("DC Dispatch Stock Snapshot", snapshot_values)

    run.revision = next_revision
    run.calculated_at = now_datetime()
    run.stock_snapshot_hash = rs._snapshot_hash(snapshot_values)
    run.calculation_input_hash = rs._calculation_input_hash(run)
    run.proposal_file = None
    run.status = "Calculated"
    run.save()

    rs.validate_current_proposal(run)

    return {
        "revision": next_revision,
        "styles": len(prepared),
        "lines": len(proposal_values),
        "suggested_qty": total_suggested,
        "warnings": sum(
            1 for value in prepared.values() if value["warning"]
        ),
    }
