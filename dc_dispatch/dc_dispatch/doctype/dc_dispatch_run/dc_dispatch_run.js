const ITEM_FIELD_METHOD =
    "dc_dispatch.dc_dispatch.doctype.dc_dispatch_run.dc_dispatch_run.get_eligible_item_fields";
const TARGET_FILTER_METHOD =
    "dc_dispatch.dc_dispatch.doctype.dc_dispatch_run.dc_dispatch_run.get_target_filter_options";
const TARGET_FILTER_FIELDS = ["item_year", "season", "collection", "drop", "main_group", "subgroup"];

frappe.ui.form.on("DC Dispatch Run", {
    onload(frm) {
        Promise.all([load_item_field_metadata(frm), refresh_target_filter_options(frm)]).catch(
            (error) => show_filter_load_error(frm, error)
        );
    },

    refresh(frm) {
        render_summary(frm);
        const can_approve = (frappe.user_roles || []).some((role) => ["Stock Manager", "System Manager"].includes(role));
        const editable = ["Draft", "Items Loaded", "Reference Review Required", "Calculated", "Proposal Imported"].includes(frm.doc.status);

        [
            "company", "sales_from_date", "sales_to_date", "minimum_match_percent",
            "reference_fields", "source_warehouse", ...TARGET_FILTER_FIELDS,
            "item_filters", "items", "store_rules",
        ].forEach((fieldname) => frm.set_df_property(fieldname, "read_only", editable ? 0 : 1));

        if (frm.is_new()) return;

        if (editable) {
            frm.add_custom_button(__("Load Eligible Stores"), () =>
                direct_doc_action(frm, "load_eligible_stores", __("Eligible stores loaded")), __("Prepare"));

            frm.add_custom_button(__("Load Target Items"), () =>
                direct_doc_action(frm, "load_target_items", __("Target items loaded")), __("Prepare"));

            frm.add_custom_button(__("Check Store History"), () =>
                check_store_history(frm), __("Prepare"));

            frm.add_custom_button(__("Cancel Run"), () => {
                frappe.confirm(
                    __("Cancel this run? Its templates will become available for another initial dispatch run."),
                    () => direct_doc_action(frm, "cancel_run", __("Run cancelled"))
                );
            }, __("Prepare"));
        }

        if (
            frm.doc.items && frm.doc.items.length &&
            frm.doc.reference_fields && frm.doc.reference_fields.length &&
            frm.doc.status !== "Cancelled"
        ) {
            frm.add_custom_button(__("Export Historical Evidence"), () => {
                open_url_post(
                    "/api/method/dc_dispatch.services.history_evidence_service.download_history_evidence",
                    {run_name: frm.doc.name}
                );
            }, __("Proposal"));
        }

        if (["Items Loaded", "Reference Review Required", "Calculated", "Proposal Imported"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Calculate Proposal"), () =>
                calculate_after_history_check(frm), __("Proposal"));
        }

        if (["Calculated", "Proposal Imported"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Export Excel"), () => {
                open_url_post("/api/method/dc_dispatch.services.excel_service.download_proposal", {run_name: frm.doc.name});
            }, __("Proposal"));

            frm.add_custom_button(__("Import Reviewed Excel"), () =>
                direct_doc_action(frm, "import_proposal", __("Reviewed proposal imported")), __("Proposal"));

            if (can_approve) {
                frm.add_custom_button(__("Approve Proposal"), () => {
                    frappe.confirm(
                        __("Approve this proposal revision?"),
                        () => direct_doc_action(frm, "approve_proposal", __("Proposal approved"))
                    );
                }, __("Proposal"));
            }
        }

        if (can_approve && ["Approved", "Material Requests Created"].includes(frm.doc.status)) {
            frm.add_custom_button(__("Create Material Requests"), () => {
                frappe.confirm(
                    __("Create Material Requests from the approved quantities?"),
                    () => direct_doc_action(frm, "create_material_requests", __("Material Requests processed"))
                );
            }, __("Execute"));
        }
    },

    item_year(frm) { reload_target_filters(frm); },
    season(frm) { reload_target_filters(frm); },
    collection(frm) { reload_target_filters(frm); },
    drop(frm) { reload_target_filters(frm); },
    main_group(frm) { reload_target_filters(frm); },
    subgroup(frm) { reload_target_filters(frm); },
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

async function direct_doc_action(frm, method, success_message) {
    try {
        const response = await frm.call(method);
        await frm.reload_doc();
        frappe.show_alert({
            message: success_message || __("Action completed"),
            indicator: "green",
        });
        return response;
    } catch (error) {
        console.error(`DC Dispatch action failed: ${method}`, error);
        throw error;
    }
}

function load_item_field_metadata(frm) {
    return frappe.call({method: ITEM_FIELD_METHOD}).then((response) => {
        frm._dc_dispatch_item_fields = response.message || [];
        apply_item_field_options(frm);
    });
}

function apply_item_field_options(frm) {
    const rows = frm._dc_dispatch_item_fields || [];
    const standard = frm._dc_dispatch_standard_item_fields || new Set();
    const reference_options = ["", ...rows.map((row) => row.fieldname)].join("\n");
    const advanced_options = ["", ...rows.filter((row) => !standard.has(row.fieldname)).map((row) => row.fieldname)].join("\n");
    frm.fields_dict.reference_fields.grid.update_docfield_property("fieldname", "options", reference_options);
    frm.fields_dict.item_filters.grid.update_docfield_property("fieldname", "options", advanced_options);
}

function refresh_target_filter_options(frm) {
    const request_id = (frm._dc_dispatch_filter_request_id || 0) + 1;
    frm._dc_dispatch_filter_request_id = request_id;
    return frappe.call({
        method: TARGET_FILTER_METHOD,
        args: Object.fromEntries(TARGET_FILTER_FIELDS.map((fieldname) => [fieldname, frm.doc[fieldname]])),
    }).then((response) => {
        if (request_id !== frm._dc_dispatch_filter_request_id) return;
        const data = response.message || {};
        const options_by_field = data.options || {};
        const configuration_errors = data.configuration_errors || [];
        if (configuration_errors.length) show_filter_load_error(frm, configuration_errors.join("<br>"));
        else frm._dc_dispatch_filter_error_shown = false;
        frm._dc_dispatch_standard_item_fields = new Set(Object.values(data.fieldnames || {}).filter(Boolean));
        apply_item_field_options(frm);
        TARGET_FILTER_FIELDS.forEach((fieldname) => {
            const values = options_by_field[fieldname] || [];
            const options = ["", ...values];
            frm.set_df_property(fieldname, "options", options.join("\n"));
            if (frm.doc[fieldname] && !options.includes(frm.doc[fieldname])) frm.set_value(fieldname, "");
        });
        const main_group_options = ["", ...(options_by_field.main_group || [])].join("\n");
        frm.fields_dict.reference_fields.grid.update_docfield_property("main_group", "options", main_group_options);
        frm.refresh_fields(TARGET_FILTER_FIELDS);
    });
}

function reload_target_filters(frm) {
    return refresh_target_filter_options(frm).catch((error) => show_filter_load_error(frm, error));
}

function show_filter_load_error(frm, error) {
    if (frm._dc_dispatch_filter_error_shown) return;
    frm._dc_dispatch_filter_error_shown = true;
    const message = typeof error === "string"
        ? error
        : __("Could not load Item filter options. Check DC Dispatch Settings and the Error Log.");
    frappe.msgprint({title: __("DC Dispatch Filter Configuration"), message, indicator: "red"});
}

function check_store_history(frm, continue_callback) {
    return frm.call("analyze_store_history").then(async (response) => {
        const data = response.message || {};
        await frm.reload_doc();

        if ((data.no_history || []).length) {
            show_no_history_dialog(frm, data, continue_callback);
        } else {
            frappe.show_alert({message: __("All included stores have historical data"), indicator: "green"});
            if (continue_callback) continue_callback();
        }
        return response;
    }).catch((error) => {
        console.error("DC Dispatch: Check Store History failed", error);
        throw error;
    });
}

function calculate_after_history_check(frm) {
    check_store_history(frm, () =>
        direct_doc_action(frm, "calculate_proposal", __("Proposal calculated")));
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
            frm.save().then(async () => {
                await frm.reload_doc();
                if (continue_callback) continue_callback();
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
