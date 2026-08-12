import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


MATERIAL_REQUEST_FIELDS = {
    "Material Request": [
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
}


def after_install():
    create_custom_fields(MATERIAL_REQUEST_FIELDS, update=True)
    _seed_settings()


def after_migrate():
    create_custom_fields(MATERIAL_REQUEST_FIELDS, update=True)
    _seed_settings()


def _seed_settings():
    settings = frappe.get_single("DC Dispatch Settings")
    defaults = {
        "warehouse_is_store_field": "custom_is_store",
        "warehouse_transit_field": "custom_transit_warehouse",
        "item_main_group_field": "custom_item_main_group",
        "item_subgroup_field": "custom_item_sub_group",
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
    legacy_is_store_field = "custom_is_store_used_in_allocation"
    warehouse_meta = frappe.get_meta("Warehouse")
    if (
        settings.warehouse_is_store_field == legacy_is_store_field
        and not warehouse_meta.get_field(legacy_is_store_field)
        and warehouse_meta.get_field("custom_is_store")
    ):
        settings.warehouse_is_store_field = "custom_is_store"
        changed = True
    if changed:
        settings.save(ignore_permissions=True)
