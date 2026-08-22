app_name = "dc_dispatch"
app_title = "DC Dispatch"
app_publisher = "Marina Trading Company"
app_description = "Sales-informed initial DC dispatch planning"
app_email = "it@marinafashion.com.sa"
app_license = "MIT"
app_version = "0.6.11"

required_apps = ["erpnext"]

after_install = "dc_dispatch.install.after_install"
after_migrate = "dc_dispatch.install.after_migrate"

doc_events = {
    "DC Dispatch Run": {
        "validate": "dc_dispatch.services.planning_guard_service.validate_run",
    },
    "Stock Entry": {
        "validate": "dc_dispatch.stock_entry_events.preserve_dispatch_route",
    }
}

doctype_js = {
    "DC Dispatch Run": "public/js/dc_dispatch_run_v063.js",
    "Stock Entry": "public/js/stock_entry_dc_dispatch_route.js",
}
