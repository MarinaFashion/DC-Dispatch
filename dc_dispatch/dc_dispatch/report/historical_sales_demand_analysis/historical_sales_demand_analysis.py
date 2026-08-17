from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from dc_dispatch.services.history_policy_service import (
    demand_sales_breakdown,
    return_audit_rows,
)
from dc_dispatch.services.metadata import STANDARD_TARGET_FILTER_FIELDS


def execute(filters=None):
    filters = frappe._dict(filters or {})
    _validate_filters(filters)

    stores = _eligible_stores(filters)
    if not stores:
        return _columns(filters.view_level), []

    run = frappe._dict(
        company=filters.company,
        sales_from_date=filters.from_date,
        sales_to_date=filters.to_date,
    )

    allowed_templates = _allowed_templates(filters)
    view_level = filters.view_level or "Store + Item Template"

    if view_level == "Return Audit":
        return _columns(view_level), _return_audit_data(run, stores, allowed_templates)

    breakdown = demand_sales_breakdown(run, stores)
    originated = _cross_store_originated(run, stores, allowed_templates)

    if view_level == "Store Summary":
        return _columns(view_level), _store_summary_data(
            stores, breakdown, originated, allowed_templates
        )

    return _columns(view_level), _template_data(
        breakdown, originated, allowed_templates
    )


def _validate_filters(filters):
    for fieldname, label in (
        ("company", _("Company")),
        ("from_date", _("From Date")),
        ("to_date", _("To Date")),
    ):
        if not filters.get(fieldname):
            frappe.throw(_("{0} is required.").format(label))

    if filters.from_date > filters.to_date:
        frappe.throw(_("From Date cannot be after To Date."))


def _eligible_stores(filters):
    if filters.get("store_warehouse"):
        warehouse = frappe.db.get_value(
            "Warehouse",
            filters.store_warehouse,
            ["name", "company", "is_group", "disabled"],
            as_dict=True,
        )
        if not warehouse:
            frappe.throw(_("Store Warehouse {0} does not exist.").format(filters.store_warehouse))
        if warehouse.company != filters.company:
            frappe.throw(_("Store Warehouse must belong to the selected Company."))
        if warehouse.is_group or warehouse.disabled:
            frappe.throw(_("Store Warehouse must be an enabled non-group warehouse."))
        return [warehouse.name]

    settings = frappe.get_single("DC Dispatch Settings")
    is_store_field = settings.warehouse_is_store_field
    warehouse_meta = frappe.get_meta("Warehouse")
    if not is_store_field or not warehouse_meta.get_field(is_store_field):
        frappe.throw(
            _("Configured Warehouse Is Store field {0} does not exist.").format(
                is_store_field or ""
            )
        )

    return frappe.get_all(
        "Warehouse",
        filters={
            "company": filters.company,
            "is_group": 0,
            "disabled": 0,
            is_store_field: 1,
        },
        pluck="name",
        order_by="name asc",
        limit_page_length=0,
    )


def _item_filter_fieldnames():
    settings = frappe.get_single("DC Dispatch Settings")
    return {
        "item_year": STANDARD_TARGET_FILTER_FIELDS["item_year"],
        "season": STANDARD_TARGET_FILTER_FIELDS["season"],
        "collection": STANDARD_TARGET_FILTER_FIELDS["collection"],
        "drop": STANDARD_TARGET_FILTER_FIELDS["drop"],
        "main_group": settings.item_main_group_field,
        "subgroup": settings.item_subgroup_field,
    }


def _allowed_templates(filters):
    item_filters = [
        ["Item", "disabled", "=", 0],
        ["Item", "has_variants", "=", 1],
    ]

    if filters.get("item_template"):
        item_filters.append(["Item", "name", "=", filters.item_template])

    item_meta = frappe.get_meta("Item")
    for filter_name, item_fieldname in _item_filter_fieldnames().items():
        value = filters.get(filter_name)
        if not value:
            continue
        if not item_fieldname or not item_meta.get_field(item_fieldname):
            frappe.throw(
                _("Item field {0} configured for {1} does not exist.").format(
                    item_fieldname or filter_name, filter_name
                )
            )
        item_filters.append(["Item", item_fieldname, "=", value])

    rows = frappe.get_all(
        "Item",
        filters=item_filters,
        fields=["name"],
        limit_page_length=0,
    )
    return {row.name for row in rows}


def _cross_store_originated(run, stores, allowed_templates):
    """Cross-store returns whose original selling warehouse is the report store.

    This is informational only and does not reduce Demand Units.
    """
    store_set = set(stores)
    totals = defaultdict(float)
    for row in return_audit_rows(run, stores):
        if row.return_classification != "Cross-Store Return - Excluded":
            continue
        if row.item_template not in allowed_templates:
            continue
        if row.original_store_warehouse not in store_set:
            continue
        key = (row.item_template, row.original_store_warehouse)
        totals[key] += flt(row.return_qty)
    return totals


def _template_data(breakdown, originated, allowed_templates):
    rows = []
    for (template, store), values in sorted(
        breakdown.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        if template not in allowed_templates:
            continue
        rows.append(
            {
                "store_warehouse": store,
                "item_template": template,
                "gross_sales_units": flt(values.get("gross_sales")),
                "same_store_returns": flt(values.get("same_store_returns")),
                "cross_store_returns_received": flt(
                    values.get("cross_store_returns_received")
                ),
                "cross_store_returns_originated": flt(
                    originated.get((template, store))
                ),
                "unresolved_returns": flt(
                    values.get("unlinked_returns_received")
                ),
                "demand_units": flt(values.get("demand_qty")),
            }
        )
    return rows


def _store_summary_data(stores, breakdown, originated, allowed_templates):
    totals = {
        store: {
            "gross_sales_units": 0.0,
            "same_store_returns": 0.0,
            "cross_store_returns_received": 0.0,
            "cross_store_returns_originated": 0.0,
            "unresolved_returns": 0.0,
            "demand_units": 0.0,
        }
        for store in stores
    }

    for (template, store), values in breakdown.items():
        if template not in allowed_templates or store not in totals:
            continue
        bucket = totals[store]
        bucket["gross_sales_units"] += flt(values.get("gross_sales"))
        bucket["same_store_returns"] += flt(values.get("same_store_returns"))
        bucket["cross_store_returns_received"] += flt(
            values.get("cross_store_returns_received")
        )
        bucket["unresolved_returns"] += flt(
            values.get("unlinked_returns_received")
        )
        bucket["demand_units"] += flt(values.get("demand_qty"))

    for (template, store), qty in originated.items():
        if template in allowed_templates and store in totals:
            totals[store]["cross_store_returns_originated"] += flt(qty)

    grand_demand = sum(max(0, row["demand_units"]) for row in totals.values())

    rows = []
    for store in sorted(totals):
        bucket = totals[store]
        demand = flt(bucket["demand_units"])
        rows.append(
            {
                "store_warehouse": store,
                **bucket,
                "demand_share_percent": (
                    (max(0, demand) / grand_demand * 100) if grand_demand else 0
                ),
            }
        )
    return rows


def _return_audit_data(run, stores, allowed_templates):
    rows = []
    for row in return_audit_rows(run, stores):
        if row.item_template not in allowed_templates:
            continue
        rows.append(
            {
                "posting_date": row.posting_date,
                "return_sales_invoice": row.return_sales_invoice,
                "item_template": row.item_template,
                "item_code": row.item_code,
                "return_store_warehouse": row.return_store_warehouse,
                "original_sales_invoice": row.original_sales_invoice,
                "original_store_warehouse": row.original_store_warehouse,
                "return_qty": flt(row.return_qty),
                "return_classification": row.return_classification,
                "resolution_method": row.resolution_method,
            }
        )
    return rows


def _columns(view_level):
    if view_level == "Return Audit":
        return [
            {"fieldname": "posting_date", "label": _("Posting Date"), "fieldtype": "Date", "width": 105},
            {"fieldname": "return_sales_invoice", "label": _("Return Sales Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 170},
            {"fieldname": "item_template", "label": _("Item Template"), "fieldtype": "Link", "options": "Item", "width": 145},
            {"fieldname": "item_code", "label": _("Item Code"), "fieldtype": "Link", "options": "Item", "width": 145},
            {"fieldname": "return_store_warehouse", "label": _("Return Warehouse"), "fieldtype": "Link", "options": "Warehouse", "width": 180},
            {"fieldname": "original_sales_invoice", "label": _("Original Sales Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 170},
            {"fieldname": "original_store_warehouse", "label": _("Original Selling Warehouse"), "fieldtype": "Link", "options": "Warehouse", "width": 190},
            {"fieldname": "return_qty", "label": _("Return Qty"), "fieldtype": "Float", "width": 100},
            {"fieldname": "return_classification", "label": _("Classification / Demand Treatment"), "fieldtype": "Data", "width": 230},
            {"fieldname": "resolution_method", "label": _("Original Link Method"), "fieldtype": "Data", "width": 220},
        ]

    common = [
        {"fieldname": "store_warehouse", "label": _("Store Warehouse"), "fieldtype": "Link", "options": "Warehouse", "width": 180},
    ]
    if view_level != "Store Summary":
        common.append(
            {"fieldname": "item_template", "label": _("Item Template"), "fieldtype": "Link", "options": "Item", "width": 145}
        )

    common.extend(
        [
            {"fieldname": "gross_sales_units", "label": _("Gross Sales Units"), "fieldtype": "Float", "width": 120},
            {"fieldname": "same_store_returns", "label": _("Same-Store Returns Deducted"), "fieldtype": "Float", "width": 165},
            {"fieldname": "cross_store_returns_received", "label": _("Cross-Store Returns Received (Excluded)"), "fieldtype": "Float", "width": 210},
            {"fieldname": "cross_store_returns_originated", "label": _("Cross-Store Returns Originated Here"), "fieldtype": "Float", "width": 205},
            {"fieldname": "unresolved_returns", "label": _("Unresolved Returns (Excluded)"), "fieldtype": "Float", "width": 170},
            {"fieldname": "demand_units", "label": _("Demand Units Used"), "fieldtype": "Float", "width": 125},
        ]
    )

    if view_level == "Store Summary":
        common.append(
            {"fieldname": "demand_share_percent", "label": _("Demand Share %"), "fieldtype": "Percent", "width": 120}
        )
    return common
