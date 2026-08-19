from __future__ import annotations

from html import escape

import frappe
from frappe import _
from frappe.utils.file_manager import save_file
from frappe.utils.pdf import get_pdf

from dc_dispatch.services.dispatch_matrix_service import (
    build_dispatch_matrix,
)


def create_and_attach_picking_list(
    run,
    material_requests,
):
    """Generate one consolidated picking list and attach it to the Run and MRs."""
    material_requests = sorted(
        {
            name
            for name in material_requests
            if name
        }
    )
    if not material_requests:
        frappe.throw(
            _("No Material Requests are available for the picking list.")
        )

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

    matrix = build_dispatch_matrix(
        run,
        lines=lines,
    )
    if not matrix["rows"]:
        frappe.throw(
            _("No final dispatch quantities exist for the picking list.")
        )

    filename = (
        f"{run.name}-R{run.revision}-"
        "Warehouse-Picking-List.pdf"
    )
    html = _render_html(
        run,
        matrix,
        material_requests,
    )

    pdf = get_pdf(
        html,
        options={
            "page-size": "A3",
            "orientation": "Landscape",
            "margin-top": "7mm",
            "margin-right": "7mm",
            "margin-bottom": "7mm",
            "margin-left": "7mm",
            "print-media-type": None,
        },
    )

    attachments = {}

    run_file = _attach_once(
        filename,
        pdf,
        "DC Dispatch Run",
        run.name,
    )
    attachments["run"] = run_file

    mr_files = {}
    for material_request in material_requests:
        mr_files[material_request] = _attach_once(
            filename,
            pdf,
            "Material Request",
            material_request,
        )
    attachments["material_requests"] = mr_files

    return {
        "filename": filename,
        "attachments": attachments,
        "stores": len(matrix["stores"]),
        "variants": len(matrix["rows"]),
        "total_qty": matrix["total_dispatched"],
    }


def _attach_once(
    filename,
    content,
    attached_to_doctype,
    attached_to_name,
):
    existing = frappe.db.get_value(
        "File",
        {
            "file_name": filename,
            "attached_to_doctype": attached_to_doctype,
            "attached_to_name": attached_to_name,
            "is_folder": 0,
        },
        ["name", "file_url"],
        as_dict=True,
    )
    if existing:
        return existing.file_url

    file_doc = save_file(
        filename,
        content,
        attached_to_doctype,
        attached_to_name,
        is_private=1,
    )
    return file_doc.file_url


def _render_html(
    run,
    matrix,
    material_requests,
):
    stores = matrix["stores"]

    store_headers = "".join(
        (
            '<th class="store">'
            + escape(store)
            + "</th>"
        )
        for store in stores
    )

    body_rows = []
    for row in matrix["rows"]:
        store_cells = "".join(
            (
                '<td class="qty">'
                + str(
                    int(
                        row["store_quantities"].get(
                            store,
                            0,
                        )
                    )
                )
                + "</td>"
            )
            for store in stores
        )

        body_rows.append(
            "<tr>"
            f'<td class="template">{escape(str(row["item_template"] or ""))}</td>'
            f'<td class="size">{escape(str(row["size"] or ""))}</td>'
            f"{store_cells}"
            f'<td class="total">{int(row["total_dispatched"])}</td>'
            f'<td class="total">{int(row["total_dc_qty"])}</td>'
            f'<td class="total">{int(row["remaining_qty"])}</td>'
            "</tr>"
        )

    total_store_cells = "".join(
        (
            '<td class="grand">'
            + str(
                sum(
                    int(
                        row["store_quantities"].get(
                            store,
                            0,
                        )
                    )
                    for row in matrix["rows"]
                )
            )
            + "</td>"
        )
        for store in stores
    )

    total_row = (
        '<tr class="grand-row">'
        '<td class="grand">Total</td>'
        '<td class="grand"></td>'
        f"{total_store_cells}"
        f'<td class="grand">{int(matrix["total_dispatched"])}</td>'
        f'<td class="grand">{int(matrix["total_dc_qty"])}</td>'
        f'<td class="grand">{int(matrix["remaining_qty"])}</td>'
        "</tr>"
    )

    mr_list = ", ".join(material_requests)

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: A3 landscape;
    margin: 7mm;
}}

body {{
    font-family: Arial, Helvetica, sans-serif;
    color: #1f1f1f;
    font-size: 8px;
}}

.title {{
    font-size: 18px;
    font-weight: bold;
    margin-bottom: 4px;
    color: #551C25;
}}

.meta {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 7px;
    font-size: 8px;
}}

.meta td {{
    padding: 2px 5px;
    border: 1px solid #d8d8d8;
}}

.meta .label {{
    font-weight: bold;
    background: #f2ebe7;
    white-space: nowrap;
}}

.dispatch {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 6px;
}}

.dispatch thead {{
    display: table-header-group;
}}

.dispatch tr {{
    page-break-inside: avoid;
}}

.dispatch th,
.dispatch td {{
    border: 0.5px solid #518b5a;
    padding: 2px 1px;
    text-align: center;
    vertical-align: middle;
    overflow-wrap: anywhere;
}}

.dispatch th {{
    background: #1f7a35;
    color: white;
    font-weight: bold;
}}

.dispatch td {{
    background: #c6efce;
}}

.dispatch tr:nth-child(odd) td {{
    background: #a9e6b2;
}}

.dispatch .template {{
    width: 23mm;
    text-align: left;
    font-weight: bold;
}}

.dispatch .size {{
    width: 9mm;
    font-weight: bold;
}}

.dispatch .store {{
    width: 12mm;
}}

.dispatch .qty {{
    width: 12mm;
}}

.dispatch .total {{
    width: 16mm;
    font-weight: bold;
}}

.dispatch .grand,
.dispatch .grand-row td {{
    background: #1f7a35 !important;
    color: white;
    font-weight: bold;
}}

.note {{
    margin-top: 6px;
    font-size: 7px;
    color: #555;
}}
</style>
</head>
<body>

<div class="title">DC DISPATCH WAREHOUSE PICKING LIST</div>

<table class="meta">
<tr>
    <td class="label">Run</td>
    <td>{escape(str(run.name))}</td>
    <td class="label">Revision</td>
    <td>{int(run.revision or 0)}</td>
    <td class="label">Source DC</td>
    <td>{escape(str(run.source_warehouse or ""))}</td>
</tr>
<tr>
    <td class="label">Stores</td>
    <td>{len(stores)}</td>
    <td class="label">Total Qty to Pick</td>
    <td>{int(matrix["total_dispatched"])}</td>
    <td class="label">Material Requests</td>
    <td>{len(material_requests)}</td>
</tr>
</table>

<table class="dispatch">
<thead>
<tr>
    <th class="template">Item Template</th>
    <th class="size">Size</th>
    {store_headers}
    <th class="total">Total Dispatched</th>
    <th class="total">Total DC Qty</th>
    <th class="total">Remaining Qty</th>
</tr>
</thead>
<tbody>
{"".join(body_rows)}
{total_row}
</tbody>
</table>

<div class="note">
Quantities are the approved Final Qty for DC Dispatch Run
{escape(str(run.name))}, revision {int(run.revision or 0)}.
Material Requests: {escape(mr_list)}
</div>

</body>
</html>
"""


def delete_generated_picking_lists(
    run_name,
    revision=None,
):
    """Optional maintenance helper for future regeneration workflows."""
    prefix = (
        f"{run_name}-R{revision}-Warehouse-Picking-List.pdf"
        if revision is not None
        else f"{run_name}-"
    )
    files = frappe.get_all(
        "File",
        filters={
            "file_name": ["like", prefix + "%"],
            "is_folder": 0,
        },
        pluck="name",
        limit_page_length=0,
    )
    for name in files:
        frappe.delete_doc(
            "File",
            name,
            ignore_permissions=True,
        )
