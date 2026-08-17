const DC_DISPATCH_DC_WAREHOUSE_QUERY =
    "dc_dispatch.services.tier_service.get_distribution_center_warehouses";
const DC_DISPATCH_APPLY_TIER_RULES_METHOD =
    "dc_dispatch.services.tier_service.apply_tier_rules";
const DC_DISPATCH_ARRANGE_NET_DEMAND_METHOD =
    "dc_dispatch.services.net_demand_priority_service.arrange_stores_by_net_demand";

frappe.ui.form.on("DC Dispatch Run", {
    setup(frm) {
        frm.set_query("source_warehouse", () => ({
            query: DC_DISPATCH_DC_WAREHOUSE_QUERY,
            filters: { company: frm.doc.company || "" },
        }));
    },

    refresh(frm) {
        const editable = [
            "Draft",
            "Items Loaded",
            "Reference Review Required",
            "Calculated",
            "Proposal Imported",
        ].includes(frm.doc.status);

        if (!frm.is_new() && editable) {
            frm.add_custom_button(
                __("Apply Tier Rules"),
                () => {
                    frappe.confirm(
                        __(
                            "Apply Tier Allocation Rules to all stores? " +
                            "This resets manual Tier overrides and Min/Max per Size."
                        ),
                        () => {
                            frappe.call({
                                method: DC_DISPATCH_APPLY_TIER_RULES_METHOD,
                                args: { run_name: frm.doc.name },
                                freeze: true,
                                freeze_message: __("Applying Tier Rules..."),
                            }).then(() => frm.reload_doc());
                        }
                    );
                },
                __("Prepare")
            );

            if ((frm.doc.store_rules || []).length) {
                frm.add_custom_button(
                    __("Arrange by Net Demand"),
                    () => {
                        frappe.confirm(
                            __(
                                "Arrange all stores from highest to lowest historical Net Demand? " +
                                "Net Demand is Gross Sales minus Same-Store Returns. " +
                                "This resets Priority to 1, 2, 3... and reapplies Tier Rules, " +
                                "replacing manual Priority/Tier overrides."
                            ),
                            () => {
                                frappe.call({
                                    method: DC_DISPATCH_ARRANGE_NET_DEMAND_METHOD,
                                    args: { run_name: frm.doc.name },
                                    freeze: true,
                                    freeze_message: __("Calculating Net Demand and arranging stores..."),
                                }).then(() => frm.reload_doc());
                            }
                        );
                    },
                    __("Prepare")
                );
            }
        }
    },
});

frappe.ui.form.on("DC Dispatch Store Rule", {
    tier(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        const rule = (frm.doc.tier_rules || []).find(
            (value) => value.tier === row.tier
        );
        if (!rule) return;

        frappe.model.set_value(
            cdt,
            cdn,
            "minimum_per_variant",
            cint(rule.minimum_per_variant || 0)
        );
        frappe.model.set_value(
            cdt,
            cdn,
            "maximum_per_style",
            cint(rule.maximum_per_variant || 0)
        );
    },
});
