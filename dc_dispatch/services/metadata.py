from __future__ import annotations

import frappe
from frappe import _

from dc_dispatch.services.filtering import cascading_options


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


STANDARD_TARGET_FILTER_FIELDS = {
    "item_year": "item_year",
    "season": "season",
    "collection": "collection",
    "drop": "custom_drop",
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


def get_target_filter_options(
    item_year=None,
    season=None,
    collection=None,
    drop=None,
    main_group=None,
    subgroup=None,
):
    """Return mutually cascading options from values used by Item Templates.

    One distinct-combination query is used for all controls.  This avoids the
    repeated unindexed Item scans that would result from querying every Select
    independently whenever a planner changes one filter.
    """
    settings = frappe.get_single("DC Dispatch Settings")
    fieldnames = {
        **STANDARD_TARGET_FILTER_FIELDS,
        "main_group": settings.item_main_group_field,
        "subgroup": settings.item_subgroup_field,
    }
    item_meta = frappe.get_meta("Item")
    missing = {
        run_fieldname: item_fieldname
        for run_fieldname, item_fieldname in fieldnames.items()
        if item_fieldname and not item_meta.get_field(item_fieldname)
    }
    resolved_fieldnames = {
        run_fieldname: (None if run_fieldname in missing else item_fieldname)
        for run_fieldname, item_fieldname in fieldnames.items()
    }

    selections = {
        "item_year": item_year,
        "season": season,
        "collection": collection,
        "drop": drop,
        "main_group": main_group,
        "subgroup": subgroup,
    }
    query_fields = sorted({fieldname for fieldname in resolved_fieldnames.values() if fieldname})
    rows = []
    if query_fields:
        rows = frappe.get_all(
            "Item",
            filters={"has_variants": 1, "disabled": 0},
            fields=query_fields,
            distinct=True,
            order_by=f"{query_fields[0]} asc",
            limit_page_length=0,
        )

    return {
        "options": cascading_options(rows, selections, resolved_fieldnames),
        "fieldnames": resolved_fieldnames,
        "configuration_errors": [
            _("{0} is mapped to missing Item field {1}.").format(
                _target_filter_label(run_fieldname), item_fieldname
            )
            for run_fieldname, item_fieldname in missing.items()
        ],
    }


def standard_target_filters(run, settings=None):
    """Map populated run controls to their real Item fieldnames."""
    settings = settings or frappe.get_single("DC Dispatch Settings")
    fieldnames = {
        **STANDARD_TARGET_FILTER_FIELDS,
        "main_group": settings.item_main_group_field,
        "subgroup": settings.item_subgroup_field,
    }
    item_meta = frappe.get_meta("Item")
    result = []
    for run_fieldname, item_fieldname in fieldnames.items():
        value = getattr(run, run_fieldname, None)
        if not value:
            continue
        if not item_fieldname or not item_meta.get_field(item_fieldname):
            frappe.throw(
                _("Configured Item filter field {0} does not exist.").format(
                    item_fieldname or run_fieldname
                )
            )
        result.append((item_fieldname, value))
    return result


def _target_filter_label(fieldname):
    return {
        "item_year": _("Item Year"),
        "season": _("Season"),
        "collection": _("Collection"),
        "drop": _("Drop / Batch"),
        "main_group": _("Main Group"),
        "subgroup": _("Item Subgroup"),
    }.get(fieldname, fieldname)
