frappe.ui.form.on("DC Dispatch Run", {
    setup(frm) {
        frappe.call({
            method: "dc_dispatch.dc_dispatch.doctype.dc_dispatch_run.dc_dispatch_run.get_eligible_item_fields",
        }).then((response) => {
            const rows = response.message || [];
            frm._dc_dispatch_item_fields = rows;
            const options = ["", ...rows.map((row) => row.fieldname)].join("\n");
            frm.fields_dict.reference_fields.grid.update_docfield_property("fieldname", "options", options);
            frm.fields_dict.item_filters.grid.update_docfield_property("fieldname", "options", options);
        });
    },

    refresh(frm) {
        render_summary(frm);
        const can_approve = (frappe.user_roles || []).some((role) => ["Stock Manager", "System Manager"].includes(role));
        const editable = ["Draft", "Items Loaded", "Reference Review Required", "Calculated", "Proposal Imported"].includes(frm.doc.status);
        [
            "company",
            "sales_from_date",
            "sales_to_date",
            "minimum_match_percent",
            "reference_fields",
            "source_warehouse",
            "item_filters",
            "items",
            "store_rules",
        ].forEach((fieldname) => frm.set_df_property(fieldname, "read_only", editable ? 0 : 1));

        if (frm.is_new()) return;

        if (editable) {
            frm.add_custom_button(__("Load Eligible Stores"), () => run_doc_action(frm, "load_eligible_stores", __("Loading stores")), __("Prepare"));
            frm.add_custom_button(__("Load Target Items"), () => run_doc_action(frm, "load_target_items", __("Loading items")), __("Prepare"));
            frm.add_custom_button(__("Check Store History"), () => check_store_history(frm), __("Prepare"));
            frm.add_custom_button(__("Cancel Run"), () => confirm_and_run(frm, "cancel_run", __("Cancel this run? Its templates will become available for another initial dispatch run.")), __("Prepare"));
        }
        if (["Items Loaded", "Reference Review Required", "Calculated", "Proposal Imported"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Calculate Proposal"), () => calculate_after_history_check(frm), __("Proposal"));
        }
        if (["Calculated", "Proposal Imported"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Export Excel"), () => {
                open_url_post("/api/method/dc_dispatch.services.excel_service.download_proposal", {run_name: frm.doc.name});
            }, __("Proposal"));
            frm.add_custom_button(__("Import Reviewed Excel"), () => run_doc_action(frm, "import_proposal", __("Validating workbook")), __("Proposal"));
            if (can_approve) {
                frm.add_custom_button(__("Approve Proposal"), () => confirm_and_run(frm, "approve_proposal", __("Approve this proposal revision?")), __("Proposal"));
            }
        }
        if (can_approve && ["Approved", "Material Requests Created"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Create Material Requests"), () => confirm_and_run(frm, "create_material_requests", __("Create Material Requests from the approved quantities?")), __("Execute"));
        }
    },
});

frappe.ui.form.on("DC Dispatch Reference Field", {
    fieldname(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        const field = (frm._dc_dispatch_item_fields || []).find((value) => value.fieldname === row.fieldname);
        if (field) frappe.model.set_value(cdt, cdn, "field_label", field.label);
    },
});

frappe.ui.form.on("DC Dispatch Item Filter", {
    fieldname(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        const field = (frm._dc_dispatch_item_fields || []).find((value) => value.fieldname === row.fieldname);
        if (field) frappe.model.set_value(cdt, cdn, "field_label", field.label);
    },
});

function run_doc_action(frm, method, freeze_message) {
    return frm.save().then(() => frm.call({
        method,
        freeze: true,
        freeze_message,
    })).then((response) => {
        frm.reload_doc();
        if (response.message) frappe.show_alert({message: __("Action completed"), indicator: "green"});
        return response;
    });
}

function confirm_and_run(frm, method, message) {
    frappe.confirm(message, () => run_doc_action(frm, method, __("Processing")));
}

function check_store_history(frm, continue_callback) {
    return frm.save().then(() => frm.call({
        method: "analyze_store_history",
        freeze: true,
        freeze_message: __("Checking historical sales by store"),
    })).then((response) => {
        const data = response.message || {};
        if ((data.no_history || []).length) {
            show_no_history_dialog(frm, data, continue_callback);
        } else {
            frappe.show_alert({message: __("All included stores have historical data"), indicator: "green"});
            frm.reload_doc().then(() => {
                if (continue_callback) continue_callback();
            });
        }
    });
}

function calculate_after_history_check(frm) {
    check_store_history(frm, () => run_doc_action(frm, "calculate_proposal", __("Calculating dispatch proposal")));
}

function show_no_history_dialog(frm, data, continue_callback) {
    const store_options = (frm.doc.store_rules || [])
        .filter((row) => row.history_status === "Has History")
        .map((row) => row.store_warehouse);
    const dialog = new frappe.ui.Dialog({
        title: __("Stores Without Historical Data"),
        size: "extra-large",
        fields: [
            {
                fieldname: "instructions",
                fieldtype: "HTML",
                options: `<p>${__("Choose whether each store should be excluded from this run or should copy the demand score of an established store. Shares will be recalculated to remain at 100%.")}</p>`,
            },
            {
                fieldname: "decisions",
                fieldtype: "Table",
                cannot_add_rows: true,
                in_place_edit: true,
                data: (data.no_history || []).map((store) => ({store_warehouse: store, decision: "Exclude"})),
                fields: [
                    {fieldname: "store_warehouse", fieldtype: "Data", label: __("Store"), in_list_view: 1, read_only: 1, columns: 4},
                    {fieldname: "decision", fieldtype: "Select", label: __("Decision"), options: "Exclude\nUse Reference Store", in_list_view: 1, reqd: 1, columns: 3},
                    {fieldname: "reference_store", fieldtype: "Select", label: __("Reference Store"), options: ["", ...store_options].join("\n"), in_list_view: 1, columns: 5},
                ],
            },
        ],
        primary_action_label: __("Apply Decisions"),
        primary_action(values) {
            for (const decision of values.decisions || []) {
                const row = (frm.doc.store_rules || []).find((value) => value.store_warehouse === decision.store_warehouse);
                if (!row) continue;
                if (decision.decision === "Use Reference Store" && !decision.reference_store) {
                    frappe.msgprint(__("Select a Reference Store for {0}.", [decision.store_warehouse]));
                    return;
                }
                frappe.model.set_value(row.doctype, row.name, "decision", decision.decision);
                frappe.model.set_value(row.doctype, row.name, "reference_store", decision.reference_store || "");
            }
            dialog.hide();
            frm.save().then(() => {
                if (continue_callback) continue_callback();
                else frm.reload_doc();
            });
        },
    });
    dialog.show();
}

function render_summary(frm) {
    const items = frm.doc.items || [];
    const stores = (frm.doc.store_rules || []).filter((row) => row.decision !== "Exclude");
    const available = items.reduce((sum, row) => sum + flt(row.dc_qty), 0);
    const target = items.reduce((sum, row) => sum + cint(row.target_qty), 0);
    const warnings = items.filter((row) => row.warning).length;
    const html = `
        <div class="row">
            <div class="col-sm-3"><b>${__("Styles")}</b><br>${items.length}</div>
            <div class="col-sm-3"><b>${__("Eligible Stores")}</b><br>${stores.length}</div>
            <div class="col-sm-3"><b>${__("Available / Target")}</b><br>${format_number(available)} / ${format_number(target)}</div>
            <div class="col-sm-3"><b>${__("Warnings")}</b><br>${warnings}</div>
        </div>`;
    if (frm.fields_dict.proposal_summary) frm.fields_dict.proposal_summary.$wrapper.html(html);
}
