from __future__ import annotations

from collections import defaultdict
import re

import frappe
from frappe import _
from frappe.utils import cint, nowdate

from dc_dispatch.services.forecast_service import (
    assert_forecast_configuration_unchanged,
)
from dc_dispatch.services.size_performance_service import (
    assert_size_configuration_unchanged,
)
from dc_dispatch.services.run_service import (
    _require_stock_manager,
    assert_calculation_inputs_unchanged,
    assert_stock_snapshot,
    validate_current_proposal,
)


def _extract_year_code(value):
    match = re.search(r"\d{2,4}", str(value or ""))
    if not match:
        frappe.throw(
            _("Item Year is required to build the Dispatch Batch Code.")
        )
    return match.group(0)[-2:]


def _extract_drop_no(value):
    match = re.search(r"\d+", str(value or ""))
    if not match:
        frappe.throw(
            _("Drop / Batch must contain a number to build the Dispatch Batch Code.")
        )
    return int(match.group(0))


def build_dispatch_batch_code(run, dispatch_group_no):
    collection = str(run.collection or "").strip()
    if not collection:
        frappe.throw(
            _("Collection is required to build the Dispatch Batch Code.")
        )

    collection_code = next(
        (
            character.upper()
            for character in collection
            if character.isalnum()
        ),
        "",
    )
    if not collection_code:
        frappe.throw(
            _("Collection must contain a letter or number.")
        )

    group_no = cint(dispatch_group_no)
    if group_no < 1:
        frappe.throw(
            _("Dispatch Group No. must be 1 or greater.")
        )

    return (
        f"{collection_code}{_extract_year_code(run.item_year)}"
        f"-D{_extract_drop_no(run.drop)}"
        f"-G{group_no}"
    )


def _required_stores(run):
    rows = frappe.get_all(
        "DC Dispatch Proposal Line",
        filters={
            "run": run.name,
            "revision": run.revision,
            "exclude": 0,
            "final_qty": [">", 0],
        },
        fields=["store_warehouse"],
        limit_page_length=0,
    )
    return {
        row.store_warehouse
        for row in rows
        if row.store_warehouse
    }


def get_material_request_creation_status(run):
    required_stores = _required_stores(run)

    active_requests = frappe.get_all(
        "Material Request",
        filters={
            "custom_dc_dispatch_run": run.name,
            "docstatus": ["<", 2],
        },
        fields=["custom_final_store_warehouse"],
        limit_page_length=0,
    )
    existing_stores = {
        row.custom_final_store_warehouse
        for row in active_requests
        if row.custom_final_store_warehouse
    }
    missing_stores = sorted(
        required_stores - existing_stores
    )

    requests_complete = bool(required_stores) and not missing_stores
    batch_code_ready = bool(
        cint(getattr(run, "dispatch_group_no", 0))
        and str(
            getattr(run, "dispatch_batch_code", "")
            or ""
        ).strip()
    )
    picking_filename = (
        f"{run.name}-R{run.revision}-"
        "Warehouse-Picking-List.pdf"
    )
    picking_list_exists = bool(
        frappe.db.exists(
            "File",
            {
                "file_name": picking_filename,
                "attached_to_doctype": "DC Dispatch Run",
                "attached_to_name": run.name,
                "is_folder": 0,
            },
        )
    )

    return {
        "required": len(required_stores),
        "existing": len(required_stores & existing_stores),
        "missing": missing_stores,
        "requests_complete": requests_complete,
        "batch_code_ready": batch_code_ready,
        "picking_list_exists": picking_list_exists,
        "complete": (
            requests_complete
            and batch_code_ready
            and picking_list_exists
        ),
    }


def suggest_dispatch_group(run):
    stored_group = cint(
        getattr(run, "dispatch_group_no", 0)
    )
    if stored_group > 0:
        return {
            "dispatch_group_no": stored_group,
            "dispatch_batch_code": build_dispatch_batch_code(
                run, stored_group
            ),
            "locked": True,
        }

    rows = frappe.get_all(
        "DC Dispatch Run",
        filters={
            "name": ["!=", run.name],
            "company": run.company,
            "item_year": run.item_year,
            "collection": run.collection,
            "drop": run.drop,
            "status": ["!=", "Cancelled"],
            "dispatch_group_no": [">", 0],
        },
        fields=["dispatch_group_no"],
        limit_page_length=0,
    )
    next_group = (
        max(
            (cint(row.dispatch_group_no) for row in rows),
            default=0,
        )
        + 1
    )
    return {
        "dispatch_group_no": next_group,
        "dispatch_batch_code": build_dispatch_batch_code(
            run, next_group
        ),
        "locked": False,
    }


def create_material_requests(run, dispatch_group_no=None):
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

    stored_group = cint(
        getattr(run, "dispatch_group_no", 0)
    )
    requested_group = cint(dispatch_group_no)

    if stored_group > 0:
        if requested_group and requested_group != stored_group:
            frappe.throw(
                _(
                    "This run already uses Dispatch Group {0}. "
                    "Retry Material Request creation with the same group."
                ).format(stored_group)
            )
        group_no = stored_group
    else:
        group_no = requested_group

    batch_code = build_dispatch_batch_code(
        run, group_no
    )

    conflict = frappe.db.get_value(
        "DC Dispatch Run",
        {
            "name": ["!=", run.name],
            "company": run.company,
            "item_year": run.item_year,
            "collection": run.collection,
            "drop": run.drop,
            "dispatch_group_no": group_no,
            "status": ["!=", "Cancelled"],
        },
        "name",
    )
    if conflict:
        frappe.throw(
            _(
                "Dispatch Batch Code {0} is already used by {1}. "
                "Choose another Dispatch Group No."
            ).format(batch_code, conflict)
        )

    frappe.db.set_value(
        "DC Dispatch Run",
        run.name,
        {
            "dispatch_group_no": group_no,
            "dispatch_batch_code": batch_code,
        },
        update_modified=True,
    )
    run.dispatch_group_no = group_no
    run.dispatch_batch_code = batch_code

    is_recovery = run.status == "Material Requests Created"

    if not is_recovery:
        assert_calculation_inputs_unchanged(run)
        assert_size_configuration_unchanged(run)
        assert_forecast_configuration_unchanged(run)
        assert_stock_snapshot(run)

    # Always validate the approved proposal itself. During recovery we
    # intentionally do not require today's stock/configuration to equal
    # the original calculation snapshot; only missing Material Requests
    # are recreated from the already-approved Final Qty.
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
            frappe.db.set_value(
                "Material Request",
                existing_request,
                "title",
                batch_code,
                update_modified=True,
            )
            existing.append(existing_request)
            _link_lines(
                store_lines,
                existing_request,
            )
            continue

        savepoint = f"dc_dispatch_mr_{index}"
        frappe.db.savepoint(savepoint)

        try:
            document = frappe.new_doc("Material Request")
            document.company = run.company
            document.material_request_type = "Material Transfer"
            document.title = batch_code
            document.transaction_date = nowdate()
            document.schedule_date = nowdate()
            document.set_from_warehouse = run.source_warehouse
            document.set_warehouse = transit
            document.custom_dc_dispatch_run = run.name
            document.custom_final_store_warehouse = store
            document.custom_dc_dispatch_instructions = (
                f"Initial dispatch generated from {run.name}, "
                f"revision {run.revision}, "
                f"batch {batch_code}. "
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

            # ERPNext may recalculate the transaction title during insert/submit.
            # Persist our dispatch batch title after the document lifecycle so
            # every created Material Request is guaranteed to display the same
            # short batch code.
            frappe.db.set_value(
                "Material Request",
                document.name,
                "title",
                batch_code,
                update_modified=False,
            )
            document.title = batch_code

            created.append(document.name)
            _link_lines(
                store_lines,
                document.name,
            )

        except Exception:
            frappe.db.rollback(
                save_point=savepoint
            )
            frappe.log_error(
                frappe.get_traceback(),
                (
                    f"DC Dispatch Run {run.name}: failed creating "
                    f"Material Request for {store}"
                ),
            )
            errors.append(store)

    all_requests = sorted(
        set(created + existing)
    )

    if all_requests:
        run.status = "Material Requests Created"
        run.flags.allow_final_status_update = True
        run.save()

    picking_list = None
    picking_list_error = None

    # Generate the warehouse picking list only when every required
    # store has a successful active Material Request.
    if (
        not errors
        and len(all_requests) == len(grouped)
    ):
        try:
            from dc_dispatch.services.picking_list_service import (
                create_and_attach_picking_list,
                delete_generated_picking_lists,
            )

            # A recovery can replace a cancelled/deleted Material Request.
            # Remove the old run/MR PDF attachments first so the regenerated
            # picking list contains the current Material Request numbers.
            delete_generated_picking_lists(
                run.name,
                run.revision,
            )
            picking_list = (
                create_and_attach_picking_list(
                    run,
                    all_requests,
                )
            )
        except Exception:
            picking_list_error = (
                "Material Requests were created successfully, "
                "but the Warehouse Picking List PDF could not "
                "be generated or attached."
            )
            frappe.log_error(
                frappe.get_traceback(),
                (
                    f"DC Dispatch Run {run.name}: failed "
                    "generating Warehouse Picking List"
                ),
            )
            frappe.msgprint(
                _(
                    picking_list_error
                    + " Run Create Material Requests again "
                    "to retry the PDF attachment."
                ),
                indicator="orange",
                alert=True,
            )

    if errors:
        frappe.msgprint(
            _(
                "Some store Material Requests could not be created: {0}. "
                "The successful stores were kept. Run Create Material Requests "
                "again after correcting the errors; existing requests will not "
                "be duplicated. The final Warehouse Picking List will be "
                "generated only after all required Material Requests exist."
            ).format(", ".join(errors)),
            indicator="orange",
            alert=True,
        )

    return {
        "created": created,
        "existing": existing,
        "errors": errors,
        "total": len(all_requests),
        "dispatch_batch_code": batch_code,
        "picking_list": picking_list,
        "picking_list_error": picking_list_error,
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
            "line_names": tuple(
                line.name
                for line in lines
            ),
        },
    )
