from __future__ import annotations

import frappe
from frappe import _


ALLOWED_FIELDTYPES = {
    "Data",
    "Select",
    "Link",
    "Dynamic Link",
    "Check",
    "Int",
    "Float",
    "Currency",
    "Percent",
    "Date",
}

EXCLUDED_ITEM_FIELDS = {
    "name",
    "owner",
    "creation",
    "modified",
    "modified_by",
    "docstatus",
    "idx",
    "item_code",
    "item_name",
    "description",
    "image",
    "variant_of",
    "has_variants",
    "disabled",
    "opening_stock",
    "valuation_rate",
    "standard_rate",
    "last_purchase_rate",
}


def get_eligible_item_fields():
    fields = []
    for field in frappe.get_meta("Item").fields:
        if (
            field.fieldname
            and field.fieldname not in EXCLUDED_ITEM_FIELDS
            and field.fieldtype in ALLOWED_FIELDTYPES
            and not field.hidden
        ):
            fields.append(
                {
                    "fieldname": field.fieldname,
                    "label": field.label or field.fieldname,
                    "fieldtype": field.fieldtype,
                    "options": field.options,
                }
            )
    return sorted(fields, key=lambda value: (value["label"].lower(), value["fieldname"]))


def validate_configured_field(doctype: str, fieldname: str, allowed_fieldtypes: set[str] | None = None):
    field = frappe.get_meta(doctype).get_field(fieldname)
    if not field:
        frappe.throw(_("Configured field {0}.{1} does not exist.").format(doctype, fieldname))
    if allowed_fieldtypes and field.fieldtype not in allowed_fieldtypes:
        frappe.throw(
            _("Configured field {0}.{1} has unsupported type {2}.").format(
                doctype, fieldname, field.fieldtype
            )
        )
    return field


def item_field_map():
    return {row["fieldname"]: row for row in get_eligible_item_fields()}
