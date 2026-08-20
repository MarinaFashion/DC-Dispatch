const DC_DISPATCH_DC_WAREHOUSE_QUERY =
    "dc_dispatch.services.tier_service.get_distribution_center_warehouses";
const DC_DISPATCH_ARRANGE_NET_DEMAND_METHOD =
    "dc_dispatch.services.net_demand_priority_service.arrange_stores_by_net_demand";
const DC_DISPATCH_ARRANGE_ITEMS_METHOD =
    "dc_dispatch.services.item_arrangement_service.arrange_items_by_avg_qty";

const DC_DISPATCH_PREPARE_ORDER = [
    "Load Target Items",
    "Load Eligible Stores",
    "Check Store History",
    "Arrange Items by Avg Qty/Variant/Store",
    "Arrange by Net Demand",
    "Cancel Run",
];

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
            if ((frm.doc.items || []).length) {
                frm.add_custom_button(
                    __("Arrange Items by Avg Qty/Variant/Store"),
                    () => {
                        frappe.call({
                            method: DC_DISPATCH_ARRANGE_ITEMS_METHOD,
                            args: { run_name: frm.doc.name },
                            freeze: true,
                            freeze_message: __("Arranging target items by average quantity..."),
                        }).then(() => frm.reload_doc());
                    },
                    __("Prepare")
                );
            }

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

        // Apply Tier Rules is intentionally not added in v0.6.1.
        // Tier defaults are already applied by the DC Dispatch Run validation
        // hook when the document is saved, so the separate menu action was
        // redundant and confusing.
        window.setTimeout(() => reorder_prepare_menu(frm), 0);
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

function reorder_prepare_menu(frm) {
    // Core DC Dispatch Run JS creates Load/Check/Cancel actions. This hooked
    // script creates the two Arrange actions. Reorder the existing menu DOM
    // only; callbacks and Frappe button registrations remain untouched.
    const buttons = DC_DISPATCH_PREPARE_ORDER
        .map((label) => frm.custom_buttons && frm.custom_buttons[__(label)])
        .filter(Boolean);

    if (!buttons.length) return;

    let menu = null;
    for (const button of buttons) {
        const candidate = button.closest(".dropdown-menu");
        if (candidate && candidate.length) {
            menu = candidate;
            break;
        }
    }
    if (!menu || !menu.length) return;

    for (const label of DC_DISPATCH_PREPARE_ORDER) {
        const button =
            frm.custom_buttons && frm.custom_buttons[__(label)];
        if (!button || !button.length) continue;

        const list_item = button.closest("li");
        if (list_item && list_item.length) {
            menu.append(list_item);
        } else {
            menu.append(button);
        }
    }
}
