from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, nowdate

from dc_dispatch.services.run_service import (
    _require_stock_manager,
    assert_calculation_inputs_unchanged,
    assert_stock_snapshot,
    validate_current_proposal,
)


def create_material_requests(run):
    _require_stock_manager()
    frappe.db.get_value(
        "DC Dispatch Run",
        run.name,
        "name",
        for_update=True,
    )

    if run.status not in {"Approved", "Material Requests Created"}:
        frappe.throw(
            _("Approve the proposal before creating Material Requests.")
        )

    assert_calculation_inputs_unchanged(run)
    assert_stock_snapshot(run)
    validate_current_proposal(run)

    settings = frappe.get_single("DC Dispatch Settings")
    lines = frappe.get_all(
        "DC Dispatch Proposal Line",
        filters={
            "run": run.name,
            "revision": run.revision,
            "exclude": 0,
            "final_qty": [">", 0],
        },
        fields=[
            "name",
            "store_warehouse",
            "transit_warehouse",
            "item_code",
            "final_qty",
        ],
        order_by="store_warehouse asc, item_code asc",
        limit_page_length=0,
    )

    if not lines:
        frappe.throw(
            _("The approved proposal has no quantities to request.")
        )

    grouped = defaultdict(list)
    for line in lines:
        grouped[
            (line.store_warehouse, line.transit_warehouse)
        ].append(line)

    created = []
    existing = []
    errors = []

    for index, ((store, transit), store_lines) in enumerate(
        grouped.items(),
        start=1,
    ):
        existing_request = frappe.db.get_value(
            "Material Request",
            {
                "custom_dc_dispatch_run": run.name,
                "custom_final_store_warehouse": store,
                "docstatus": ["<", 2],
            },
            "name",
        )
        if existing_request:
            existing.append(existing_request)
            _link_lines(store_lines, existing_request)
            continue

        savepoint = f"dc_dispatch_mr_{index}"
        frappe.db.savepoint(savepoint)

        try:
            document = frappe.new_doc("Material Request")
            document.company = run.company
            document.material_request_type = "Material Transfer"
            document.transaction_date = nowdate()
            document.schedule_date = nowdate()
            document.set_from_warehouse = run.source_warehouse
            document.set_warehouse = transit
            document.custom_dc_dispatch_run = run.name
            document.custom_final_store_warehouse = store
            document.custom_dc_dispatch_instructions = (
                f"Initial dispatch generated from {run.name}, "
                f"revision {run.revision}. "
                f"Ship through {transit} to final store {store}."
            )

            for line in store_lines:
                document.append(
                    "items",
                    {
                        "item_code": line.item_code,
                        "qty": cint(line.final_qty),
                        "from_warehouse": run.source_warehouse,
                        "warehouse": transit,
                        "schedule_date": nowdate(),
                    },
                )

            document.insert()
            if settings.auto_submit_material_requests:
                document.submit()

            created.append(document.name)
            _link_lines(store_lines, document.name)

        except Exception:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(
                frappe.get_traceback(),
                (
                    f"DC Dispatch Run {run.name}: failed creating "
                    f"Material Request for {store}"
                ),
            )
            errors.append(store)

    if created or existing:
        run.status = "Material Requests Created"
        run.flags.allow_final_status_update = True
        run.save()

    if errors:
        frappe.msgprint(
            _(
                "Some store Material Requests could not be created: {0}. "
                "The successful stores were kept. Run Create Material Requests "
                "again after correcting the errors; existing requests will not "
                "be duplicated."
            ).format(", ".join(errors)),
            indicator="orange",
            alert=True,
        )

    return {
        "created": created,
        "existing": existing,
        "errors": errors,
        "total": len(created) + len(existing),
    }


def _link_lines(lines, material_request):
    if not lines:
        return

    frappe.db.sql(
        """
        UPDATE `tabDC Dispatch Proposal Line`
        SET material_request = %(material_request)s
        WHERE name IN %(line_names)s
        """,
        {
            "material_request": material_request,
            "line_names": tuple(line.name for line in lines),
        },
    )
