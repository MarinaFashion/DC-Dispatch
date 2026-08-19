from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from dc_dispatch.services import run_service as rs


@frappe.whitelist()
def arrange_items_by_avg_qty(run_name):
    """Arrange target items by Avg Qty per Variant/Store, highest first."""
    run = frappe.get_doc("DC Dispatch Run", run_name)
    rs._require_editable(run)
    rs._require_saved(run)

    if not run.items:
        frappe.throw(_("Load target items first."))

    eligible_store_count = sum(
        1 for row in run.store_rules if row.decision != "Exclude"
    )
    if eligible_store_count <= 0:
        frappe.throw(_("At least one eligible store is required."))

    for row in run.items:
        available_variants = cint(row.available_variant_count or 0)
        if available_variants > 0:
            average = (
                flt(row.dc_qty)
                / available_variants
                / eligible_store_count
            )
        else:
            average = 0

        row.avg_qty_per_variant_store = average
        row.avg_dispatch_qty_per_variant_store = (
            average * flt(row.dispatch_percentage) / 100
        )

    ordered = sorted(
        list(run.items),
        key=lambda row: (
            -flt(row.avg_qty_per_variant_store),
            row.item_template or "",
        ),
    )

    run.items = ordered
    for index, row in enumerate(run.items, start=1):
        row.idx = index

    run.save()

    return {
        "items": len(run.items),
        "eligible_stores": eligible_store_count,
        "ranking": [
            {
                "item_template": row.item_template,
                "avg_qty_per_variant_store": flt(
                    row.avg_qty_per_variant_store
                ),
            }
            for row in run.items
        ],
    }
