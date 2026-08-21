import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


# Warehouse metadata required by DC Dispatch.
#
# DC Dispatch owns only the classification fields below. The transit warehouse
# link itself is ERPNext's standard Warehouse.default_in_transit_warehouse
# field and is intentionally NOT duplicated as a custom field.
#
# These classification fieldnames intentionally match Marina's Stock Allocation
# app. If that app has already created them, create_custom_fields(update=True)
# reuses/updates the same Custom Field records instead of creating duplicates.
WAREHOUSE_FIELDS = [
    {
        "fieldname": "stock_alloc_section",
        "label": "Stock Allocation",
        "fieldtype": "Section Break",
        "insert_after": "warehouse_type",
        "collapsible": 1,
    },
    {
        "fieldname": "custom_is_store",
        "label": "Is Store (used in Allocation)",
        "fieldtype": "Check",
        "insert_after": "stock_alloc_section",
        "description": (
            "Marks this warehouse as a retail store eligible to receive/source "
            "stock in allocation and dispatch runs."
        ),
    },
    {
        "fieldname": "custom_is_distribution_center",
        "label": "Is Distribution Center (used in Allocation)",
        "fieldtype": "Check",
        "insert_after": "custom_is_store",
        "description": (
            "Marks this warehouse as a distribution center/source warehouse "
            "for allocation and DC Dispatch."
        ),
    },
    {
        "fieldname": "custom_is_transit",
        "label": "Is Transit Warehouse (used in Allocation)",
        "fieldtype": "Check",
        "insert_after": "custom_is_distribution_center",
        "description": (
            "Marks this warehouse as a transit warehouse used between the "
            "distribution center and the final store."
        ),
    },
]


TRACE_FIELDS = [
    {
        "fieldname": "custom_dc_dispatch_run",
        "label": "DC Dispatch Run",
        "fieldtype": "Link",
        "options": "DC Dispatch Run",
        "insert_after": "material_request_type",
        "read_only": 1,
        "no_copy": 1,
    },
    {
        "fieldname": "custom_final_store_warehouse",
        "label": "Final Store Warehouse",
        "fieldtype": "Link",
        "options": "Warehouse",
        "insert_after": "custom_dc_dispatch_run",
        "read_only": 1,
        "no_copy": 1,
    },
    {
        "fieldname": "custom_dc_dispatch_instructions",
        "label": "DC Dispatch Instructions",
        "fieldtype": "Small Text",
        "insert_after": "custom_final_store_warehouse",
        "read_only": 1,
        "no_copy": 1,
    },
]


CUSTOM_FIELDS = {
    "Warehouse": WAREHOUSE_FIELDS,
    "Material Request": TRACE_FIELDS,
    "Stock Entry": TRACE_FIELDS,
}


def after_install():
    _ensure_custom_fields()
    _seed_settings()


def after_migrate():
    _ensure_custom_fields()
    _seed_settings()


def _ensure_custom_fields():
    """Create/update DC Dispatch metadata without deleting existing values."""
    create_custom_fields(
        CUSTOM_FIELDS,
        update=True,
    )


def _seed_settings():
    settings = frappe.get_single("DC Dispatch Settings")
    defaults = {
        "warehouse_is_store_field": "custom_is_store",
        "warehouse_transit_field": "default_in_transit_warehouse",
        "item_main_group_field": "custom_item_main_group",
        "item_subgroup_field": "item_sub_group",
        "item_related_set_field": "custom_related_set",
        "default_dispatch_percentage": 80,
        "minimum_cohort_templates": 5,
        "minimum_cohort_units": 50,
        "minimum_cohort_stores": 5,
    }

    changed = False
    for fieldname, value in defaults.items():
        if not settings.get(fieldname):
            settings.set(fieldname, value)
            changed = True

    warehouse_meta = frappe.get_meta("Warehouse")

    legacy_is_store_field = "custom_is_store_used_in_allocation"
    if (
        settings.warehouse_is_store_field == legacy_is_store_field
        and not warehouse_meta.get_field(legacy_is_store_field)
        and warehouse_meta.get_field("custom_is_store")
    ):
        settings.warehouse_is_store_field = "custom_is_store"
        changed = True

    # v0.6.9 and earlier used a custom transit link. From v0.6.10 onward,
    # always use ERPNext's standard Warehouse.default_in_transit_warehouse.
    if (
        settings.warehouse_transit_field == "custom_transit_warehouse"
        and warehouse_meta.get_field("default_in_transit_warehouse")
    ):
        settings.warehouse_transit_field = "default_in_transit_warehouse"
        changed = True

    legacy_subgroup_field = "custom_item_sub_group"
    item_meta = frappe.get_meta("Item")
    if (
        settings.item_subgroup_field == legacy_subgroup_field
        and not item_meta.get_field(legacy_subgroup_field)
        and item_meta.get_field("item_sub_group")
    ):
        settings.item_subgroup_field = "item_sub_group"
        changed = True

    if changed:
        settings.save(ignore_permissions=True)
