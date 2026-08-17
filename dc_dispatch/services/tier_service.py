from __future__ import annotations

import frappe
from frappe import _


EDITABLE_STATUSES = {
    "Draft",
    "Items Loaded",
    "Reference Review Required",
    "Calculated",
    "Proposal Imported",
}


def validate_run(doc, method=None):
    _validate_source_warehouse(doc)
    _validate_tier_rules(doc)
    _validate_store_limits(doc)

    # v0.5.0 retires Advanced Item Filters. Old rows are removed when a run is saved.
    if getattr(doc, "item_filters", None):
        doc.set("item_filters", [])

    # Apply priority-based defaults once after eligible stores are first loaded.
    # Afterwards, planner edits to Store Tier remain authoritative.
    if (
        doc.store_rules
        and doc.tier_rules
        and not int(doc.tier_defaults_applied or 0)
    ):
        _apply_rules(doc, require_full_coverage=True)
        doc.tier_defaults_applied = 1


def _validate_source_warehouse(doc):
    if not doc.source_warehouse:
        return

    warehouse = frappe.db.get_value(
        "Warehouse",
        doc.source_warehouse,
        ["company", "is_group", "disabled"],
        as_dict=True,
    )
    if not warehouse:
        frappe.throw(_("Source DC Warehouse does not exist."))
    if warehouse.company != doc.company:
        frappe.throw(_("Source DC Warehouse must belong to the selected Company."))
    if warehouse.is_group or warehouse.disabled:
        frappe.throw(_("Source DC Warehouse must be an enabled non-group Warehouse."))

    dc_field = distribution_center_fieldname()
    if not frappe.db.get_value("Warehouse", doc.source_warehouse, dc_field):
        frappe.throw(
            _("Source DC Warehouse {0} is not marked as a Distribution Center.").format(
                doc.source_warehouse
            )
        )


def _validate_tier_rules(doc):
    seen_tiers = set()
    ranges = []

    for row in doc.tier_rules:
        if row.tier in seen_tiers:
            frappe.throw(_("Duplicate Tier Allocation Rule for Tier {0}.").format(row.tier))
        seen_tiers.add(row.tier)

        start = int(row.priority_from or 0)
        end = int(row.priority_to or 0)
        minimum = int(row.minimum_per_variant or 0)
        maximum = int(row.maximum_per_variant or 0)

        if start <= 0 or end <= 0 or start > end:
            frappe.throw(_("Tier {0} has an invalid Priority From/To range.").format(row.tier))
        if minimum < 0 or maximum < 0:
            frappe.throw(_("Tier Min/Max Qty per Size cannot be negative."))
        if maximum and maximum < minimum:
            frappe.throw(
                _("Tier {0} Maximum Qty per Size cannot be below Minimum Qty per Size.").format(
                    row.tier
                )
            )
        ranges.append((start, end, row.tier))

    ranges.sort()
    for index in range(1, len(ranges)):
        previous = ranges[index - 1]
        current = ranges[index]
        if current[0] <= previous[1]:
            frappe.throw(
                _("Tier priority ranges overlap: {0} and {1}.").format(
                    previous[2], current[2]
                )
            )


def _validate_store_limits(doc):
    for row in doc.store_rules:
        minimum = int(row.minimum_per_variant or 0)
        maximum = int(row.maximum_per_style or 0)
        if minimum < 0 or maximum < 0:
            frappe.throw(
                _("Store Min/Max per Size cannot be negative for {0}.").format(
                    row.store_warehouse
                )
            )
        if maximum and maximum < minimum:
            frappe.throw(
                _("Maximum per Size cannot be below Minimum per Size for {0}.").format(
                    row.store_warehouse
                )
            )


def _rule_for_priority(rules, priority):
    priority = int(priority or 0)
    for rule in rules:
        if int(rule.priority_from or 0) <= priority <= int(rule.priority_to or 0):
            return rule
    return None


def _apply_rules(doc, require_full_coverage):
    uncovered = []
    for row in doc.store_rules:
        rule = _rule_for_priority(doc.tier_rules, row.priority)
        if not rule:
            uncovered.append(row.store_warehouse)
            continue

        row.tier = rule.tier
        row.minimum_per_variant = int(rule.minimum_per_variant or 0)
        # Kept as maximum_per_style only for DB migration compatibility.
        # From v0.5.0 onward, its business meaning is Maximum Qty per Size.
        row.maximum_per_style = int(rule.maximum_per_variant or 0)

    if require_full_coverage and uncovered:
        frappe.throw(
            _("No Tier priority range covers these stores: {0}").format(
                ", ".join(uncovered[:50])
            )
        )


@frappe.whitelist()
def apply_tier_rules(run_name):
    doc = frappe.get_doc("DC Dispatch Run", run_name)
    if doc.status not in EDITABLE_STATUSES:
        frappe.throw(_("This DC Dispatch Run is no longer editable."))
    if not doc.tier_rules:
        frappe.throw(_("Add Tier Allocation Rules first."))

    _validate_tier_rules(doc)
    _apply_rules(doc, require_full_coverage=True)
    doc.tier_defaults_applied = 1
    doc.save()
    return {"stores": len(doc.store_rules)}


def distribution_center_fieldname():
    meta = frappe.get_meta("Warehouse")

    for fieldname in (
        "custom_is_distribution_center_used_in_allocation",
        "custom_is_distribution_center",
    ):
        field = meta.get_field(fieldname)
        if field and field.fieldtype == "Check":
            return fieldname

    expected_label = "is distribution center (used in allocation)"
    for field in meta.fields:
        if (
            field.fieldtype == "Check"
            and str(field.label or "").strip().lower() == expected_label
        ):
            return field.fieldname

    frappe.throw(
        _(
            'Warehouse must have a Check field labeled '
            '"Is Distribution Center (used in Allocation)".'
        )
    )


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_distribution_center_warehouses(
    doctype,
    txt,
    searchfield,
    start,
    page_len,
    filters,
):
    company = (filters or {}).get("company")
    dc_field = distribution_center_fieldname()

    warehouse_filters = {
        "is_group": 0,
        "disabled": 0,
        dc_field: 1,
    }
    if company:
        warehouse_filters["company"] = company
    if txt:
        warehouse_filters["name"] = ["like", "%" + txt + "%"]

    rows = frappe.get_all(
        "Warehouse",
        filters=warehouse_filters,
        fields=["name"],
        order_by="name asc",
        limit_start=start,
        limit_page_length=page_len,
    )
    return [[row.name] for row in rows]
