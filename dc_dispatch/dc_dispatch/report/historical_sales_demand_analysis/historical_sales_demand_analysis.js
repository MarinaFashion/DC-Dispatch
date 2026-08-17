frappe.query_reports["Historical Sales & Demand Analysis"] = {
    filters: [
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
            reqd: 1,
            default: frappe.defaults.get_user_default("Company"),
        },
        {
            fieldname: "from_date",
            label: __("From Date"),
            fieldtype: "Date",
            reqd: 1,
            default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
        },
        {
            fieldname: "to_date",
            label: __("To Date"),
            fieldtype: "Date",
            reqd: 1,
            default: frappe.datetime.get_today(),
        },
        {
            fieldname: "view_level",
            label: __("View Level"),
            fieldtype: "Select",
            options: "Store + Item Template\nStore Summary\nReturn Audit",
            default: "Store + Item Template",
            reqd: 1,
        },
        {
            fieldname: "store_warehouse",
            label: __("Store Warehouse"),
            fieldtype: "Link",
            options: "Warehouse",
            get_query: () => {
                const company = frappe.query_report.get_filter_value("company");
                return {
                    filters: {
                        company: company,
                        is_group: 0,
                        disabled: 0,
                    },
                };
            },
        },
        {
            fieldname: "item_template",
            label: __("Item Template"),
            fieldtype: "Link",
            options: "Item",
            get_query: () => ({
                filters: {
                    has_variants: 1,
                    disabled: 0,
                },
            }),
        },
        {fieldname: "item_year", label: __("Item Year"), fieldtype: "Data"},
        {fieldname: "season", label: __("Season"), fieldtype: "Data"},
        {fieldname: "collection", label: __("Collection"), fieldtype: "Data"},
        {fieldname: "drop", label: __("Drop / Batch"), fieldtype: "Data"},
        {fieldname: "main_group", label: __("Main Group"), fieldtype: "Data"},
        {fieldname: "subgroup", label: __("Item Subgroup"), fieldtype: "Data"},
    ],
};
