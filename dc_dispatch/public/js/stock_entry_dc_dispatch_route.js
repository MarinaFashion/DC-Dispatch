frappe.ui.form.on("Stock Entry", {
    refresh(frm) {
        if (!frm.is_new() || !frm.doc.items || !frm.doc.items.length) {
            return;
        }

        const mr_names = [
            ...new Set(
                frm.doc.items
                    .map(row => row.material_request)
                    .filter(Boolean)
            )
        ];

        if (mr_names.length !== 1) {
            return;
        }

        frappe.db.get_value(
            "Material Request",
            mr_names[0],
            [
                "custom_dc_dispatch_run",
                "custom_final_store_warehouse",
                "custom_dc_dispatch_instructions",
                "set_from_warehouse",
                "set_warehouse"
            ]
        ).then(result => {
            const mr = result && result.message;
            if (!mr || !mr.custom_dc_dispatch_run) {
                return;
            }

            frm.set_value(
                "from_warehouse",
                mr.set_from_warehouse || null
            );
            frm.set_value(
                "to_warehouse",
                mr.set_warehouse || null
            );

            if (frm.fields_dict.custom_dc_dispatch_run) {
                frm.set_value(
                    "custom_dc_dispatch_run",
                    mr.custom_dc_dispatch_run
                );
            }
            if (frm.fields_dict.custom_final_store_warehouse) {
                frm.set_value(
                    "custom_final_store_warehouse",
                    mr.custom_final_store_warehouse
                );
            }
            if (frm.fields_dict.custom_dc_dispatch_instructions) {
                frm.set_value(
                    "custom_dc_dispatch_instructions",
                    mr.custom_dc_dispatch_instructions
                );
            }

            (frm.doc.items || []).forEach(row => {
                if (row.material_request === mr_names[0]) {
                    frappe.model.set_value(
                        row.doctype,
                        row.name,
                        "s_warehouse",
                        mr.set_from_warehouse
                    );
                    frappe.model.set_value(
                        row.doctype,
                        row.name,
                        "t_warehouse",
                        mr.set_warehouse
                    );
                }
            });

            frm.refresh_field("items");
        });
    }
});
