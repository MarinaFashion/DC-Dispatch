from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint


def build_dispatch_matrix(run, lines=None):
    """Build the planner/warehouse matrix from current proposal Final Qty.

    The matrix is shared by:
      - Excel "Simple Allocation" sheet
      - Warehouse Picking List PDF

    Quantity policy:
      excluded line -> 0
      otherwise     -> Final Qty
    """
    if lines is None:
        lines = frappe.get_all(
            "DC Dispatch Proposal Line",
            filters={
                "run": run.name,
                "revision": run.revision,
            },
            fields=[
                "item_template",
                "item_code",
                "store_warehouse",
                "final_qty",
                "exclude",
            ],
            limit_page_length=0,
        )

    active_store_rows = [
        row
        for row in run.store_rules
        if row.decision != "Exclude"
    ]
    active_store_rows.sort(
        key=lambda row: (
            int(row.priority or 0)
            if int(row.priority or 0) > 0
            else 999999,
            int(row.idx or 0),
            row.store_warehouse or "",
        )
    )
    stores = [row.store_warehouse for row in active_store_rows]

    item_order = {
        row.item_template: index
        for index, row in enumerate(run.items)
    }

    item_codes = sorted(
        {
            row.item_code
            for row in lines
            if row.item_code
        }
    )
    size_by_item = _variant_size_map(item_codes)

    qty_by_key = defaultdict(int)
    variant_info = {}

    for row in lines:
        quantity = 0 if row.exclude else cint(row.final_qty)

        qty_by_key[
            (row.item_code, row.store_warehouse)
        ] += quantity

        variant_info[row.item_code] = {
            "item_template": row.item_template,
            "size": (
                size_by_item.get(row.item_code)
                or _size_from_item_code(
                    row.item_template,
                    row.item_code,
                )
            ),
        }

    snapshot_rows = frappe.get_all(
        "DC Dispatch Stock Snapshot",
        filters={
            "run": run.name,
            "revision": run.revision,
        },
        fields=["item_code", "actual_qty"],
        limit_page_length=0,
    )
    dc_qty_by_item = {
        row.item_code: cint(row.actual_qty)
        for row in snapshot_rows
    }

    ordered_item_codes = sorted(
        variant_info,
        key=lambda item_code: (
            item_order.get(
                variant_info[item_code]["item_template"],
                999999,
            ),
            variant_info[item_code]["item_template"] or "",
            _size_sort_key(
                variant_info[item_code]["size"]
            ),
            item_code,
        ),
    )

    rows = []
    for item_code in ordered_item_codes:
        info = variant_info[item_code]

        store_quantities = {
            store: cint(
                qty_by_key.get((item_code, store), 0)
            )
            for store in stores
        }

        total_dispatched = sum(
            store_quantities.values()
        )
        total_dc_qty = cint(
            dc_qty_by_item.get(item_code, 0)
        )

        rows.append(
            {
                "item_template": info["item_template"],
                "item_code": item_code,
                "size": info["size"],
                "store_quantities": store_quantities,
                "total_dispatched": total_dispatched,
                "total_dc_qty": total_dc_qty,
                "remaining_qty": (
                    total_dc_qty - total_dispatched
                ),
            }
        )

    return {
        "stores": stores,
        "rows": rows,
        "total_dispatched": sum(
            row["total_dispatched"]
            for row in rows
        ),
        "total_dc_qty": sum(
            row["total_dc_qty"]
            for row in rows
        ),
        "remaining_qty": sum(
            row["remaining_qty"]
            for row in rows
        ),
    }


def _variant_size_map(item_codes):
    if not item_codes:
        return {}

    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={
            "parent": ["in", item_codes],
        },
        fields=[
            "parent",
            "attribute",
            "attribute_value",
        ],
        limit_page_length=0,
    )

    result = {}
    for row in rows:
        if (
            str(row.attribute or "")
            .strip()
            .casefold()
            == "size"
            and row.attribute_value
        ):
            result[row.parent] = row.attribute_value

    return result


def _size_from_item_code(item_template, item_code):
    """Safe fallback only when Item Variant Attribute Size is unavailable."""
    template = str(item_template or "")
    code = str(item_code or "")

    prefix = template + "-"
    if template and code.startswith(prefix):
        return code[len(prefix):]

    return code


def _size_sort_key(value):
    text = str(value or "").strip().upper()

    standard = {
        "XXXS": 1,
        "XXS": 2,
        "XS": 3,
        "S": 4,
        "M": 5,
        "L": 6,
        "XL": 7,
        "XXL": 8,
        "2XL": 8,
        "XXXL": 9,
        "3XL": 9,
        "XXXXL": 10,
        "4XL": 10,
        "XXXXXL": 11,
        "5XL": 11,
    }

    if text in standard:
        return (0, standard[text], text)

    try:
        return (1, float(text), text)
    except (TypeError, ValueError):
        return (2, 0, text)
