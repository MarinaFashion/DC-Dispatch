app_name = "dc_dispatch"
app_title = "DC Dispatch"
app_publisher = "Marina Trading Company"
app_description = "Sales-informed initial DC dispatch planning"
app_email = "it@marinafashion.com.sa"
app_license = "MIT"
app_version = "0.6.17"

required_apps = ["erpnext"]

after_install = "dc_dispatch.install.after_install"
after_migrate = "dc_dispatch.install.after_migrate"

doc_events = {
    "DC Dispatch Run": {
        "validate": "dc_dispatch.services.planning_guard_service.validate_run",
    },
    "Material Request": {
        "on_cancel": "dc_dispatch.material_request_events.clear_proposal_links",
        "on_trash": "dc_dispatch.material_request_events.clear_proposal_links",
    }
}

doctype_js = {
    "DC Dispatch Run": "public/js/dc_dispatch_run_v063.js",
}
