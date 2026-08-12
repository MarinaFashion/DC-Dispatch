from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from math import floor

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from dc_dispatch.services.allocation import (
    StoreInput,
    allocate_style,
    choose_related_set_stores,
    validate_related_sets,
)
from dc_dispatch.services.metadata import ALLOWED_FIELDTYPES, item_field_map, validate_configured_field


EDITABLE_STATUSES = {"Draft", "Items Loaded", "Reference Review Required", "Calculated", "Proposal Imported"}


def load_eligible_stores(run):
    _require_editable(run)
    settings = _settings_and_validate()
    is_store_field = settings.warehouse_is_store_field
    transit_field = settings.warehouse_transit_field
    existing = {row.store_warehouse: row.as_dict() for row in run.store_rules}
    rows = frappe.get_all(
        "Warehouse",
        filters={
            "company": run.company,
            "is_group": 0,
            "disabled": 0,
            is_store_field: 1,
        },
        fields=["name", transit_field],
        order_by="name asc",
        limit_page_length=0,
    )
    run.set("store_rules", [])
    missing_transit = []
    for index, row in enumerate(rows, start=1):
        if row.name == run.source_warehouse:
            continue
        transit = row.get(transit_field)
        if not transit or not frappe.db.exists(
            "Warehouse", {"name": transit, "company": run.company, "is_group": 0, "disabled": 0}
        ):
            missing_transit.append(row.name)
            continue
        previous = existing.get(row.name, {})
        run.append(
            "store_rules",
            {
                "store_warehouse": row.name,
                "transit_warehouse": transit,
                "decision": previous.get("decision") or "Include",
                "reference_store": previous.get("reference_store"),
                "tier": previous.get("tier") or "B",
                "priority": previous.get("priority") or index,
                "minimum_per_variant": previous.get("minimum_per_variant") or 1,
                "maximum_per_style": previous.get("maximum_per_style") or 0,
                "history_status": "Pending",
            },
        )
    if not run.store_rules:
        frappe.throw(_("No eligible stores were found using Warehouse field {0}.").format(is_store_field))
    if missing_transit:
        frappe.throw(
            _("These store warehouses have no valid transit warehouse: {0}").format(
                ", ".join(missing_transit)
            )
        )
    run.save()
    return {"stores": len(run.store_rules)}


def load_target_items(run):
    _require_editable(run)
    settings = _settings_and_validate()
    allowed_fields = item_field_map()
    filters = [["Item", "disabled", "=", 0]]
    for row in run.item_filters:
        if row.fieldname not in allowed_fields:
            frappe.throw(_("Item filter field {0} is not allowed.").format(row.fieldname))
        filters.append(["Item", row.fieldname, row.operator, _parse_filter_value(row.operator, row.value)])

    required_fields = {
        "name",
        "variant_of",
        settings.item_main_group_field,
        settings.item_subgroup_field,
        settings.item_related_set_field,
    }
    item_rows = frappe.get_all(
        "Item",
        filters=filters,
        fields=[field for field in required_fields if field],
        order_by="name asc",
        limit_page_length=0,
    )
    templates = [row for row in item_rows if not row.variant_of]
    if not templates:
        frappe.throw(_("No Item Templates matched the target filters."))

    stock_by_template = get_variant_stock_bulk([row.name for row in templates], run.source_warehouse)
    templates = [row for row in templates if sum(stock_by_template.get(row.name, {}).values()) > 0]
    if not templates:
        frappe.throw(_("The matched templates have no available stock in {0}.").format(run.source_warehouse))
    missing_main_group = [
        row.name for row in templates if not row.get(settings.item_main_group_field)
    ]
    if missing_main_group:
        frappe.throw(
            _("These target templates have no Item Main Group: {0}").format(
                ", ".join(missing_main_group[:50])
            )
        )
    _lock_item_templates([row.name for row in templates])
    _validate_not_dispatched_elsewhere(run, [row.name for row in templates])

    existing = {row.item_template: row.as_dict() for row in run.items}
    run.set("items", [])
    for row in templates:
        previous = existing.get(row.name, {})
        available = sum(stock_by_template[row.name].values())
        dispatch_percent = previous.get("dispatch_percentage") or settings.default_dispatch_percentage or 80
        run.append(
            "items",
            {
                "item_template": row.name,
                "main_group": row.get(settings.item_main_group_field),
                "subgroup": row.get(settings.item_subgroup_field),
                "related_set": row.get(settings.item_related_set_field),
                "dc_qty": available,
                "dispatch_percentage": dispatch_percent,
                "target_qty": _round_whole(available * flt(dispatch_percent) / 100),
            },
        )
    run.status = "Items Loaded"
    run.save()
    return {"items": len(run.items), "available_qty": sum(flt(row.dc_qty) for row in run.items)}


def analyze_store_history(run):
    _require_saved(run)
    if not run.store_rules:
        frappe.throw(_("Load eligible stores first."))
    stores = [row.store_warehouse for row in run.store_rules]
    sales = _historical_sales(run, stores)
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
            elif row.decision == "Use Reference Store" and max(0, totals[row.reference_store]) <= 0:
                row.history_status = "Reference Store Missing"
                no_history.append(row.store_warehouse)
    run.status = "Reference Review Required" if no_history else (run.status if run.status != "Draft" else "Items Loaded")
    run.save()
    return {
        "no_history": no_history,
        "stores": [
            {
                "store": row.store_warehouse,
                "net_units": max(0, totals[row.store_warehouse]),
                "status": row.history_status,
                "decision": row.decision,
                "reference_store": row.reference_store,
            }
            for row in run.store_rules
        ],
    }


def calculate_proposal(run):
    _require_editable(run)
    _require_saved(run)
    if not run.items:
        frappe.throw(_("Load the target items before calculating."))
    if not run.reference_fields:
        frappe.throw(_("Select at least one historical matching field."))
    if not run.store_rules:
        frappe.throw(_("Load eligible stores before calculating."))
    _settings_and_validate()
    _validate_reference_fields(run)
    _lock_item_templates([row.item_template for row in run.items])
    _validate_not_dispatched_elsewhere(run, [row.item_template for row in run.items])
    history = analyze_store_history(run)
    if history["no_history"]:
        frappe.throw(
            _("Resolve stores without history by choosing Exclude or Use Reference Store: {0}").format(
                ", ".join(history["no_history"])
            )
        )

    settings = frappe.get_single("DC Dispatch Settings")
    _validate_related_set_members(run, settings)
    stock_by_template = get_variant_stock_bulk(
        [row.item_template for row in run.items], run.source_warehouse
    )
    sales = _historical_sales(
        run, [row.store_warehouse for row in run.store_rules if row.decision != "Exclude"]
    )
    candidate_templates = {template for template, _store in sales}
    target_templates = {row.item_template for row in run.items}
    value_fields = _reference_fieldnames(run) | {
        settings.item_main_group_field,
        settings.item_subgroup_field,
        settings.item_related_set_field,
    }
    template_values = _item_values(candidate_templates | target_templates, value_fields)
    fields_by_group = _fields_by_main_group(run)

    prepared = {}
    related_members = defaultdict(list)
    for item_row in run.items:
        stock = stock_by_template.get(item_row.item_template, {})
        current_total = sum(stock.values())
        if current_total != floor(flt(item_row.dc_qty)):
            item_row.dc_qty = current_total
        target_total = min(current_total, _round_whole(current_total * flt(item_row.dispatch_percentage) / 100))
        item_row.target_qty = target_total
        scores, evidence, cohort = _cohort_scores(
            item_row,
            template_values,
            sales,
            fields_by_group,
            settings.item_main_group_field,
            flt(run.minimum_match_percent),
        )
        adjusted_scores, missing_references = _adjust_store_scores(run, scores)
        warning = _evidence_warning(evidence, settings)
        selected_fields = fields_by_group.get(item_row.main_group, [])
        missing_target_fields = [
            field for field in selected_fields if not _has_value(template_values[item_row.item_template].get(field))
        ]
        if missing_target_fields:
            warning = "; ".join(
                value
                for value in [warning, "Target item is blank for: " + ", ".join(missing_target_fields)]
                if value
            )
        if missing_references:
            reference_warning = "Reference store has no demand for this cohort: " + ", ".join(missing_references)
            warning = "; ".join(value for value in [warning, reference_warning] if value)
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
            related_members[item_row.related_set].append(item_row.item_template)

    store_rules = {row.store_warehouse: row for row in run.store_rules if row.decision != "Exclude"}
    allowed_by_template: dict[str, set[str] | None] = {template: None for template in prepared}
    set_scores: dict[str, dict[str, float]] = {}
    for related_set, members in related_members.items():
        combined_scores = defaultdict(float)
        member_stock = {}
        member_targets = {}
        for template in members:
            prepared_item = prepared[template]
            score_total = sum(prepared_item["scores"].values())
            for store, score in prepared_item["scores"].items():
                combined_scores[store] += score / score_total if score_total else 0
            member_stock[template] = prepared_item["stock"]
            member_targets[template] = prepared_item["target"]
        set_store_inputs = _store_inputs(store_rules, combined_scores)
        allowed = choose_related_set_stores(member_stock, member_targets, set_store_inputs)
        if not allowed:
            frappe.throw(_("Related Set {0} cannot cover a complete size bundle for any store.").format(related_set))
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
            set_scores[item_row.related_set] if item_row.related_set else prepared_item["scores"]
        )
        store_inputs = _store_inputs(store_rules, allocation_scores)
        allocation = allocate_style(
            prepared_item["stock"],
            prepared_item["target"],
            store_inputs,
            allowed_stores=allowed_by_template[template],
        )
        score_total = sum(max(0, value) for value in allocation_scores.values())
        for warehouse, rule in store_rules.items():
            for item_code in allocation.variant_targets:
                suggested = allocation.quantities.get(warehouse, {}).get(item_code, 0)
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
                        "sales_score": allocation_scores.get(warehouse, 0),
                        "share_percent": (
                            allocation_scores.get(warehouse, 0) * 100 / score_total if score_total else 0
                        ),
                        "suggested_qty": suggested,
                        "final_qty": suggested,
                        "exclude": 0,
                        "validation_status": "Warning" if prepared_item["warning"] else "Valid",
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

    _bulk_insert("DC Dispatch Proposal Line", proposal_values)
    _bulk_insert("DC Dispatch Stock Snapshot", snapshot_values)
    run.revision = next_revision
    run.calculated_at = now_datetime()
    run.stock_snapshot_hash = _snapshot_hash(snapshot_values)
    run.calculation_input_hash = _calculation_input_hash(run)
    run.proposal_file = None
    run.status = "Calculated"
    run.save()
    validate_current_proposal(run)
    return {
        "revision": next_revision,
        "styles": len(prepared),
        "lines": len(proposal_values),
        "suggested_qty": total_suggested,
        "warnings": sum(1 for value in prepared.values() if value["warning"]),
    }


def approve_proposal(run):
    _require_stock_manager()
    if run.status not in {"Calculated", "Proposal Imported"}:
        frappe.throw(_("Only a calculated or imported proposal can be approved."))
    assert_calculation_inputs_unchanged(run)
    assert_stock_snapshot(run)
    validate_current_proposal(run)
    run.status = "Approved"
    run.save()
    return {"status": run.status}


def validate_current_proposal(run):
    lines = frappe.get_all(
        "DC Dispatch Proposal Line",
        filters={"run": run.name, "revision": run.revision},
        fields=[
            "store_warehouse",
            "item_template",
            "item_code",
            "related_set",
            "final_qty",
            "exclude",
        ],
        limit_page_length=0,
    )
    expected_members = defaultdict(set)
    for row in run.items:
        if row.related_set:
            expected_members[row.related_set].add(row.item_template)
    errors = validate_related_sets(lines, expected_members)
    if errors:
        frappe.throw("<br>".join(errors[:20]))

    final_by_variant = defaultdict(int)
    final_by_template = defaultdict(int)
    for line in lines:
        quantity = 0 if line.exclude else int(line.final_qty or 0)
        if quantity < 0:
            frappe.throw(_("Final quantities cannot be negative."))
        final_by_variant[line.item_code] += quantity
        final_by_template[line.item_template] += quantity
    snapshots = {
        row.item_code: floor(flt(row.actual_qty))
        for row in frappe.get_all(
            "DC Dispatch Stock Snapshot",
            filters={"run": run.name, "revision": run.revision},
            fields=["item_code", "actual_qty"],
            limit_page_length=0,
        )
    }
    for item_code, quantity in final_by_variant.items():
        if quantity > snapshots.get(item_code, 0):
            frappe.throw(_("Final quantity for {0} exceeds the DC stock snapshot.").format(item_code))
    targets = {row.item_template: int(row.target_qty or 0) for row in run.items}
    for template, quantity in final_by_template.items():
        if quantity > targets.get(template, 0):
            frappe.throw(_("Final quantity for {0} exceeds its dispatch target.").format(template))


def assert_stock_snapshot(run):
    snapshots = frappe.get_all(
        "DC Dispatch Stock Snapshot",
        filters={"run": run.name, "revision": run.revision},
        fields=["item_code", "actual_qty"],
        limit_page_length=0,
    )
    if not snapshots:
        frappe.throw(_("No stock snapshot exists for this proposal revision."))
    current = _bin_quantities([row.item_code for row in snapshots], run.source_warehouse)
    changed = []
    for row in snapshots:
        old = floor(flt(row.actual_qty))
        new = floor(max(0, flt(current.get(row.item_code, 0))))
        if old != new:
            changed.append(f"{row.item_code}: {old} → {new}")
    if changed:
        frappe.throw(
            _("DC stock changed after calculation. Recalculate the proposal.<br>{0}").format(
                "<br>".join(changed[:50])
            )
        )


def assert_calculation_inputs_unchanged(run):
    if not run.calculation_input_hash or run.calculation_input_hash != _calculation_input_hash(run):
        frappe.throw(_("Run criteria changed after calculation. Recalculate the proposal before continuing."))


def get_variant_stock_bulk(templates: list[str], warehouse: str):
    if not templates:
        return {}
    rows = frappe.get_all(
        "Item",
        filters=[
            ["Item", "disabled", "=", 0],
            ["Item", "variant_of", "in", templates],
        ],
        fields=["name", "variant_of"],
        limit_page_length=0,
    )
    template_by_item = {row.name: row.variant_of for row in rows}
    for template in templates:
        if template not in template_by_item:
            template_by_item[template] = template
    quantities = _bin_quantities(list(template_by_item), warehouse)
    result = {template: {} for template in templates}
    for item_code, template in template_by_item.items():
        quantity = floor(max(0, flt(quantities.get(item_code, 0))))
        if quantity > 0:
            result.setdefault(template, {})[item_code] = quantity
    return result


def _historical_sales(run, stores: list[str]):
    if not stores:
        return {}
    has_original_link = bool(frappe.get_meta("Sales Invoice Item").get_field("sales_invoice_item"))
    original_join = (
        "LEFT JOIN `tabSales Invoice Item` original_item ON original_item.name = sii.sales_invoice_item"
        if has_original_link
        else ""
    )
    warehouse_expression = (
        "CASE WHEN si.is_return = 1 THEN COALESCE(original_item.warehouse, sii.warehouse) ELSE sii.warehouse END"
        if has_original_link
        else "sii.warehouse"
    )
    rows = frappe.db.sql(
        f"""
        SELECT
            COALESCE(NULLIF(item.variant_of, ''), item.name) AS item_template,
            {warehouse_expression} AS store_warehouse,
            SUM(sii.qty) AS net_qty
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent AND si.docstatus = 1
        INNER JOIN `tabItem` item ON item.name = sii.item_code
        {original_join}
        WHERE si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND {warehouse_expression} IN %(stores)s
        GROUP BY COALESCE(NULLIF(item.variant_of, ''), item.name), {warehouse_expression}
        """,
        {
            "company": run.company,
            "from_date": run.sales_from_date,
            "to_date": run.sales_to_date,
            "stores": tuple(stores),
        },
        as_dict=True,
    )
    return {(row.item_template, row.store_warehouse): flt(row.net_qty) for row in rows}


def _cohort_scores(item_row, values, sales, fields_by_group, main_group_field, threshold):
    target = values.get(item_row.item_template, {})
    group = target.get(main_group_field) or item_row.main_group
    selected_fields = fields_by_group.get(group, [])
    comparable_fields = [field for field in selected_fields if _has_value(target.get(field))]
    cohort = set()
    for candidate in {template for template, _store in sales}:
        candidate_values = values.get(candidate, {})
        if candidate_values.get(main_group_field) != group:
            continue
        if not comparable_fields:
            match_percent = 100
        else:
            matches = sum(
                1
                for field in comparable_fields
                if _has_value(candidate_values.get(field))
                and _normal(candidate_values.get(field)) == _normal(target.get(field))
            )
            match_percent = matches * 100 / len(comparable_fields)
        if match_percent >= threshold:
            cohort.add(candidate)

    scores = defaultdict(float)
    for (template, store), quantity in sales.items():
        if template in cohort:
            scores[store] += quantity
    scores = {store: max(0, quantity) for store, quantity in scores.items()}
    evidence = {
        "templates": len(cohort),
        "units": sum(scores.values()),
        "stores": sum(1 for quantity in scores.values() if quantity > 0),
    }
    return scores, evidence, sorted(cohort)


def _adjust_store_scores(run, scores):
    adjusted = {}
    missing_references = []
    for rule in run.store_rules:
        if rule.decision == "Exclude":
            continue
        if rule.decision == "Use Reference Store":
            adjusted[rule.store_warehouse] = max(0, flt(scores.get(rule.reference_store, 0)))
            if adjusted[rule.store_warehouse] <= 0:
                missing_references.append(f"{rule.store_warehouse} → {rule.reference_store}")
        else:
            adjusted[rule.store_warehouse] = max(0, flt(scores.get(rule.store_warehouse, 0)))
    return adjusted, missing_references


def _store_inputs(store_rules, scores):
    return [
        StoreInput(
            warehouse=warehouse,
            score=max(0, flt(scores.get(warehouse, 0))),
            tier=rule.tier,
            priority=rule.priority,
            minimum_per_variant=rule.minimum_per_variant,
            maximum_per_style=rule.maximum_per_style,
        )
        for warehouse, rule in store_rules.items()
    ]


def _validate_related_set_members(run, settings):
    fieldname = settings.item_related_set_field
    if not fieldname:
        return
    selected_by_set = defaultdict(set)
    for row in run.items:
        if row.related_set:
            selected_by_set[row.related_set].add(row.item_template)
    for related_set, selected in selected_by_set.items():
        all_templates = frappe.get_all(
            "Item",
            filters={fieldname: related_set, "variant_of": ["in", ["", None]], "disabled": 0},
            pluck="name",
            limit_page_length=0,
        )
        stock = get_variant_stock_bulk(all_templates, run.source_warehouse)
        required = {template for template in all_templates if sum(stock.get(template, {}).values()) > 0}
        missing = required - selected
        if missing:
            frappe.throw(
                _("Related Set {0} is incomplete. Add these in-stock templates: {1}").format(
                    related_set, ", ".join(sorted(missing))
                )
            )


def _validate_reference_fields(run):
    allowed = item_field_map()
    settings = frappe.get_single("DC Dispatch Settings")
    for row in run.reference_fields:
        if row.fieldname not in allowed:
            frappe.throw(_("Matching field {0} is not eligible.").format(row.fieldname))
        row.field_label = allowed[row.fieldname]["label"]
    groups = {row.main_group for row in run.items}
    configured = {row.main_group for row in run.reference_fields}
    missing = sorted(group for group in groups if group and group not in configured)
    if missing:
        frappe.throw(_("No historical matching fields are configured for: {0}").format(", ".join(missing)))
    validate_configured_field("Item", settings.item_main_group_field, ALLOWED_FIELDTYPES)


def _settings_and_validate():
    settings = frappe.get_single("DC Dispatch Settings")
    validate_configured_field("Warehouse", settings.warehouse_is_store_field, {"Check"})
    validate_configured_field("Warehouse", settings.warehouse_transit_field, {"Link"})
    validate_configured_field("Item", settings.item_main_group_field, ALLOWED_FIELDTYPES)
    if settings.item_subgroup_field:
        validate_configured_field("Item", settings.item_subgroup_field, ALLOWED_FIELDTYPES)
    if settings.item_related_set_field:
        validate_configured_field("Item", settings.item_related_set_field, ALLOWED_FIELDTYPES)
    return settings


def _fields_by_main_group(run):
    result = defaultdict(list)
    for row in run.reference_fields:
        result[row.main_group].append(row.fieldname)
    return result


def _reference_fieldnames(run):
    return {row.fieldname for row in run.reference_fields}


def _item_values(templates: set[str], fields: set[str]):
    if not templates:
        return {}
    rows = frappe.get_all(
        "Item",
        filters={"name": ["in", list(templates)]},
        fields=["name", *sorted(field for field in fields if field)],
        limit_page_length=0,
    )
    return {row.name: row for row in rows}


def _evidence_warning(evidence, settings):
    warnings = []
    if evidence["templates"] < int(settings.minimum_cohort_templates or 0):
        warnings.append(f"Only {evidence['templates']} matching historical templates")
    if evidence["units"] < flt(settings.minimum_cohort_units):
        warnings.append(f"Only {evidence['units']:g} net units")
    if evidence["stores"] < int(settings.minimum_cohort_stores or 0):
        warnings.append(f"Sales in only {evidence['stores']} stores")
    return "; ".join(warnings)


def _validate_not_dispatched_elsewhere(run, templates):
    if not templates:
        return
    conflicts = frappe.db.sql(
        """
        SELECT DISTINCT child.item_template, child.parent
        FROM `tabDC Dispatch Run Item` child
        INNER JOIN `tabDC Dispatch Run` parent ON parent.name = child.parent
        WHERE child.item_template IN %(templates)s
          AND parent.name != %(run)s
          AND parent.status NOT IN ('Cancelled')
        """,
        {"templates": tuple(templates), "run": run.name or ""},
        as_dict=True,
    )
    if conflicts:
        details = ", ".join(f"{row.item_template} ({row.parent})" for row in conflicts[:30])
        frappe.throw(_("These templates already belong to another initial dispatch run: {0}").format(details))


def _lock_item_templates(templates):
    if templates:
        frappe.db.sql(
            "SELECT name FROM `tabItem` WHERE name IN %(templates)s ORDER BY name FOR UPDATE",
            {"templates": tuple(templates)},
        )


def _bin_quantities(item_codes, warehouse):
    if not item_codes:
        return {}
    rows = frappe.get_all(
        "Bin",
        filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
        fields=["item_code", "actual_qty"],
        limit_page_length=0,
    )
    return {row.item_code: flt(row.actual_qty) for row in rows}


def _parse_filter_value(operator, value):
    if operator in {"in", "not in", "between"}:
        parts = [part.strip() for part in str(value).split(",") if part.strip()]
        if operator == "between" and len(parts) != 2:
            frappe.throw(_("The between operator requires exactly two comma-separated values."))
        return parts
    return value


def _bulk_insert(doctype, rows):
    if not rows:
        return
    fields = list(rows[0])
    standard_fields = ["name", "creation", "modified", "modified_by", "owner", "docstatus", "idx"]
    now = now_datetime()
    all_fields = standard_fields + fields
    values = []
    for index, row in enumerate(rows, start=1):
        values.append(
            [
                frappe.generate_hash(length=10),
                now,
                now,
                frappe.session.user,
                frappe.session.user,
                0,
                index,
                *[row.get(field) for field in fields],
            ]
        )
    frappe.db.bulk_insert(doctype, fields=all_fields, values=values)


def _snapshot_hash(rows):
    payload = sorted((row["item_code"], floor(flt(row["actual_qty"]))) for row in rows)
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _calculation_input_hash(run):
    payload = {
        "company": run.company,
        "sales_from_date": str(run.sales_from_date or ""),
        "sales_to_date": str(run.sales_to_date or ""),
        "minimum_match_percent": flt(run.minimum_match_percent),
        "source_warehouse": run.source_warehouse,
        "reference_fields": [
            [row.main_group, row.fieldname] for row in sorted(run.reference_fields, key=lambda value: value.idx)
        ],
        "item_filters": [
            [row.fieldname, row.operator, row.value]
            for row in sorted(run.item_filters, key=lambda value: value.idx)
        ],
        "items": [
            [row.item_template, flt(row.dispatch_percentage)]
            for row in sorted(run.items, key=lambda value: value.idx)
        ],
        "stores": [
            [
                row.store_warehouse,
                row.transit_warehouse,
                row.decision,
                row.reference_store,
                row.tier,
                int(row.priority or 0),
                int(row.minimum_per_variant or 0),
                int(row.maximum_per_style or 0),
            ]
            for row in sorted(run.store_rules, key=lambda value: value.idx)
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _round_whole(value):
    return floor(flt(value) + 0.5)


def _normal(value):
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def _has_value(value):
    return value not in (None, "")


def _require_saved(run):
    if run.is_new():
        frappe.throw(_("Save the DC Dispatch Run first."))


def _require_editable(run):
    if run.status not in EDITABLE_STATUSES:
        frappe.throw(_("This run is no longer editable."))


def _require_stock_manager():
    roles = set(frappe.get_roles())
    if not roles.intersection({"Stock Manager", "System Manager"}):
        frappe.throw(_("Stock Manager permission is required for this action."), frappe.PermissionError)
