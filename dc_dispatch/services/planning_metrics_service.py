from __future__ import annotations

import frappe
from frappe.utils import flt

from dc_dispatch.services import run_service as rs


@frappe.whitelist()
def refresh_item_planning_metrics(run_name):
    """Refresh informational item planning metrics without changing allocation logic."""
    run = frappe.get_doc("DC Dispatch Run", run_name)
    rs._require_editable(run)
    rs._require_saved(run)

    if not run.items:
        return {"items": 0, "eligible_stores": 0}

    eligible_store_count = sum(
        1 for row in run.store_rules if row.decision != "Exclude"
    )

    templates = [row.item_template for row in run.items if row.item_template]
    image_by_template = {
        row.name: row.image
        for row in frappe.get_all(
            "Item",
            filters={"name": ["in", templates]},
            fields=["name", "image"],
            limit_page_length=0,
        )
    }

    stock_by_template = rs.get_variant_stock_bulk(
        templates,
        run.source_warehouse,
    )

    for row in run.items:
        stock = stock_by_template.get(row.item_template, {})
        available_variants = sum(
            1 for quantity in stock.values() if flt(quantity) > 0
        )

        row.item_image_url = image_by_template.get(row.item_template)
        row.available_variant_count = available_variants

        if available_variants > 0 and eligible_store_count > 0:
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

    run.save()

    return {
        "items": len(run.items),
        "eligible_stores": eligible_store_count,
    }
