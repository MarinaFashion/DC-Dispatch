from __future__ import annotations

from collections import defaultdict
from math import floor

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

import dc_dispatch.services.run_service as rs


# ------------------------------
# Demand extraction
# ------------------------------

def _sales_invoice_item_has_field(fieldname: str) -> bool:
    return bool(frappe.get_meta("Sales Invoice Item").get_field(fieldname))


def _gross_sales_rows(run, stores: list[str]):
    if not stores:
        return []

    return frappe.db.sql(
        """
        SELECT
            COALESCE(NULLIF(item.variant_of, ''), item.name) AS item_template,
            sii.warehouse AS store_warehouse,
            SUM(sii.qty) AS gross_sales
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si
            ON si.name = sii.parent AND si.docstatus = 1
        INNER JOIN `tabItem` item
            ON item.name = sii.item_code
        WHERE si.company = %(company)s
          AND si.is_return = 0
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


def _return_rows(run, return_stores: list[str] | None = None):
    """Return all submitted return lines in the run period.

    Resolution order is intentionally:
      1. Sales Invoice Item.sales_invoice_item exact child-row link, when present.
      2. Sales Invoice.return_against + item_code fallback.
      3. If multiple original rows exist, accept the fallback only when all
         matching rows point to the same warehouse; otherwise leave unresolved.
    """
    has_item_link = _sales_invoice_item_has_field("sales_invoice_item")
    item_link_select = (
        "sii.sales_invoice_item AS linked_sales_invoice_item"
        if has_item_link
        else "NULL AS linked_sales_invoice_item"
    )

    return_store_clause = ""
    params = {
        "company": run.company,
        "from_date": run.sales_from_date,
        "to_date": run.sales_to_date,
    }
    if return_stores:
        return_store_clause = "AND sii.warehouse IN %(return_stores)s"
        params["return_stores"] = tuple(return_stores)

    rows = frappe.db.sql(
        f"""
        SELECT
            si.name AS return_sales_invoice,
            si.return_against,
            si.posting_date,
            sii.name AS return_sales_invoice_item,
            sii.item_code,
            sii.warehouse AS return_store_warehouse,
            ABS(sii.qty) AS return_qty,
            COALESCE(NULLIF(item.variant_of, ''), item.name) AS item_template,
            {item_link_select}
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si
            ON si.name = sii.parent AND si.docstatus = 1
        INNER JOIN `tabItem` item
            ON item.name = sii.item_code
        WHERE si.company = %(company)s
          AND si.is_return = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          {return_store_clause}
        ORDER BY si.posting_date, si.name, sii.idx
        """,
        params,
        as_dict=True,
    )

    if not rows:
        return []

    exact_names = {
        row.linked_sales_invoice_item
        for row in rows
        if row.linked_sales_invoice_item
    }
    return_against_names = {
        row.return_against
        for row in rows
        if row.return_against
    }

    original_filters = []
    if exact_names:
        original_filters.append(["name", "in", list(exact_names)])
    if return_against_names:
        original_filters.append(["parent", "in", list(return_against_names)])

    original_rows = []
    if original_filters:
        # OR the exact-child and return-against candidate sets in SQL so we can
        # resolve all return rows without an N+1 query pattern.
        clauses = []
        params = {}
        if exact_names:
            clauses.append("sii.name IN %(exact_names)s")
            params["exact_names"] = tuple(exact_names)
        if return_against_names:
            clauses.append("sii.parent IN %(return_against_names)s")
            params["return_against_names"] = tuple(return_against_names)

        original_rows = frappe.db.sql(
            f"""
            SELECT
                sii.name,
                sii.parent,
                sii.item_code,
                sii.warehouse,
                sii.qty,
                sii.uom
            FROM `tabSales Invoice Item` sii
            WHERE {' OR '.join(clauses)}
            """,
            params,
            as_dict=True,
        )

    by_name = {row.name: row for row in original_rows}
    by_invoice_item = defaultdict(list)
    for row in original_rows:
        by_invoice_item[(row.parent, row.item_code)].append(row)

    resolved = []
    for row in rows:
        original = None
        resolution_method = None

        if row.linked_sales_invoice_item:
            original = by_name.get(row.linked_sales_invoice_item)
            if original:
                resolution_method = "Sales Invoice Item Link"

        if original is None and row.return_against:
            candidates = by_invoice_item.get(
                (row.return_against, row.item_code), []
            )
            if len(candidates) == 1:
                original = candidates[0]
                resolution_method = "Return Against + Item Code"
            elif len(candidates) > 1:
                warehouses = {
                    candidate.warehouse
                    for candidate in candidates
                    if candidate.warehouse
                }
                if len(warehouses) == 1:
                    original = candidates[0]
                    resolution_method = (
                        "Return Against + Item Code (Warehouse Unambiguous)"
                    )

        original_warehouse = original.warehouse if original else None
        original_invoice = (
            original.parent
            if original
            else (row.return_against or None)
        )

        if original_warehouse:
            classification = (
                "Same-Store Return - Deducted"
                if original_warehouse == row.return_store_warehouse
                else "Cross-Store Return - Excluded"
            )
        else:
            classification = "Unresolved Return - Excluded"
            if row.return_against:
                resolution_method = (
                    resolution_method
                    or "Return Against Present - Original Item Ambiguous"
                )
            else:
                resolution_method = resolution_method or "No Return Against"

        resolved.append(
            frappe._dict(
                {
                    **row,
                    "original_sales_invoice": original_invoice,
                    "original_store_warehouse": original_warehouse,
                    "return_classification": classification,
                    "resolution_method": resolution_method,
                }
            )
        )

    return resolved


def demand_sales_breakdown(run, stores: list[str]):
    """Return historical demand components by Item Template and store.

    Demand policy:
        Demand Units = Gross Sales - Same-Store Returns

    Return origin resolution:
        exact Sales Invoice Item link first; if blank, fall back to
        Sales Invoice.return_against + item_code. Cross-store returns remain
        excluded from demand because the original store made the sale and the
        returned stock physically moved to another store.
    """
    if not stores:
        return {}

    result = {}
    for row in _gross_sales_rows(run, stores):
        key = (row.item_template, row.store_warehouse)
        result[key] = {
            "gross_sales": flt(row.gross_sales),
            "same_store_returns": 0.0,
            "cross_store_returns_received": 0.0,
            "unlinked_returns_received": 0.0,
            "demand_qty": flt(row.gross_sales),
        }

    store_set = set(stores)
    for row in _return_rows(run, stores):
        return_store = row.return_store_warehouse
        original_store = row.original_store_warehouse

        if row.return_classification == "Same-Store Return - Deducted":
            if return_store not in store_set:
                continue
            key = (row.item_template, return_store)
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
            bucket["same_store_returns"] += flt(row.return_qty)

        elif row.return_classification == "Cross-Store Return - Excluded":
            if return_store not in store_set:
                continue
            key = (row.item_template, return_store)
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
            bucket["cross_store_returns_received"] += flt(row.return_qty)

        else:
            if return_store not in store_set:
                continue
            key = (row.item_template, return_store)
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
            bucket["unlinked_returns_received"] += flt(row.return_qty)

    for bucket in result.values():
        bucket["demand_qty"] = (
            flt(bucket["gross_sales"])
            - flt(bucket["same_store_returns"])
        )

    return result


def demand_historical_sales(run, stores: list[str], allowed_templates: set[str] | None = None):
    """Return the demand quantity map consumed by cohort scoring."""
    breakdown = demand_sales_breakdown(run, stores)
    result = {}
    for key, values in breakdown.items():
        template = key[0]
        if allowed_templates is not None and template not in allowed_templates:
            continue
        demand_qty = flt(values["demand_qty"])
        if demand_qty != 0:
            result[key] = demand_qty
    return result


def return_audit_rows(run, stores: list[str]):
    """Return line-level return records for the evidence workbook."""
    if not stores:
        return []

    store_set = set(stores)
    return [
        row
        for row in _return_rows(run)
        if row.return_store_warehouse in store_set
        or row.original_store_warehouse in store_set
    ]


# ------------------------------
# Historical reference filter logic
# ------------------------------

def historical_filter_rows(run):
    return list(getattr(run, "historical_reference_filters", None) or [])


def historical_filter_fieldnames(run):
    return {row.fieldname for row in historical_filter_rows(run) if row.fieldname}


def _applicable_historical_filters(run, main_group: str | None):
    rows = []
    for row in historical_filter_rows(run):
        row_group = (row.main_group or "").strip()
        if not row_group or row_group == (main_group or ""):
            rows.append(row)
    return rows


def _split_filter_values(value):
    return [part.strip() for part in cstr(value).split(",") if part.strip()]


def cstr(value):
    return "" if value is None else str(value)


def _coerce_for_compare(value):
    text = cstr(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except Exception:
        return text.lower()


def _compare_between(actual_value, filter_value):
    parts = [part.strip() for part in cstr(filter_value).split(",", 1)]
    if len(parts) != 2:
        return False
    low = _coerce_for_compare(parts[0])
    high = _coerce_for_compare(parts[1])
    actual = _coerce_for_compare(actual_value)
    if actual is None or low is None or high is None:
        return False
    if isinstance(actual, float) and isinstance(low, float) and isinstance(high, float):
        return low <= actual <= high
    actual = cstr(actual)
    return cstr(low) <= actual <= cstr(high)


def _matches_filter(actual_value, operator, filter_value):
    operator = (operator or "=").strip()
    actual_normal = rs._normal(actual_value)

    if operator == "=":
        return actual_normal == rs._normal(filter_value)
    if operator == "!=":
        return actual_normal != rs._normal(filter_value)
    if operator == "in":
        return actual_normal in {rs._normal(value) for value in _split_filter_values(filter_value)}
    if operator == "not in":
        return actual_normal not in {rs._normal(value) for value in _split_filter_values(filter_value)}
    if operator == "like":
        return rs._normal(filter_value) in actual_normal
    if operator == "between":
        return _compare_between(actual_value, filter_value)
    return False


def template_matches_historical_scope(run, target_main_group: str | None, template: str, template_values: dict):
    rows = _applicable_historical_filters(run, target_main_group)
    if not rows:
        return True
    values = template_values.get(template, {})
    for row in rows:
        if not row.fieldname:
            continue
        actual_value = values.get(row.fieldname)
        if not _matches_filter(actual_value, row.operator, row.value):
            return False
    return True


def historical_scope_candidates(run, item_row, candidate_templates: set[str], template_values: dict):
    return {
        template
        for template in candidate_templates
        if template_matches_historical_scope(
            run,
            getattr(item_row, "main_group", None),
            template,
            template_values,
        )
    }


def historical_scope_text(run, target_main_group: str | None, field_labels: dict | None = None):
    rows = _applicable_historical_filters(run, target_main_group)
    if not rows:
        return "All historical items within the selected sales date range"

    parts = []
    for row in rows:
        label = (field_labels or {}).get(row.fieldname, row.field_label or row.fieldname)
        prefix = f"[{row.main_group}] " if row.main_group else ""
        parts.append(f"{prefix}{label} {row.operator or '='} {row.value}")
    return " ; ".join(parts)


def _allowed_templates_for_history_check(run, candidate_templates: set[str], template_values: dict):
    if getattr(run, "items", None):
        allowed = set()
        for item_row in run.items:
            allowed.update(
                historical_scope_candidates(
                    run,
                    item_row,
                    candidate_templates,
                    template_values,
                )
            )
        return allowed

    return {
        template
        for template in candidate_templates
        if template_matches_historical_scope(run, None, template, template_values)
    }


# ------------------------------
# Main actions
# ------------------------------

def analyze_store_history(run):
    """Store-history check using the demand-history policy and scope filters."""
    rs._require_saved(run)
    if not run.store_rules:
        frappe.throw(_("Load eligible stores first."))

    stores = [row.store_warehouse for row in run.store_rules]
    breakdown = demand_sales_breakdown(run, stores)
    candidate_templates = {template for template, _store in breakdown}
    settings = frappe.get_single("DC Dispatch Settings")
    value_fields = historical_filter_fieldnames(run) | {settings.item_main_group_field}
    template_values = rs._item_values(candidate_templates, value_fields) if candidate_templates else {}
    allowed_templates = _allowed_templates_for_history_check(run, candidate_templates, template_values)

    totals = defaultdict(float)
    for (template, store), values in breakdown.items():
        if template not in allowed_templates:
            continue
        totals[store] += max(0, flt(values["demand_qty"]))

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

    included_stores = [
        row.store_warehouse
        for row in run.store_rules
        if row.decision != "Exclude"
    ]

    stock_by_template = rs.get_variant_stock_bulk(
        [row.item_template for row in run.items],
        run.source_warehouse,
    )
    sales = demand_historical_sales(run, included_stores)

    candidate_templates = {template for template, _store in sales}
    target_templates = {row.item_template for row in run.items}
    value_fields = rs._reference_fieldnames(run) | historical_filter_fieldnames(run) | {
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

        scoped_templates = historical_scope_candidates(
            run,
            item_row,
            candidate_templates,
            template_values,
        )
        scoped_sales = {
            key: qty
            for key, qty in sales.items()
            if key[0] in scoped_templates
        }

        scores, evidence, cohort = rs._cohort_scores(
            item_row,
            template_values,
            scoped_sales,
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

        if not scoped_templates:
            scope_warning = "No historical templates passed the Historical Reference Filters"
            warning = "; ".join(
                value for value in [warning, scope_warning] if value
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
