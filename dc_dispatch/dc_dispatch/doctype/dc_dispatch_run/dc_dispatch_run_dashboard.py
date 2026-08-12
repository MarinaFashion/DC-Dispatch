from frappe import _


def get_data():
    return {
        "fieldname": "custom_dc_dispatch_run",
        "transactions": [
            {"label": _("Execution"), "items": ["Material Request"]},
        ],
    }
