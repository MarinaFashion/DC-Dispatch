from __future__ import annotations

import frappe


def clear_proposal_links(doc, method=None):
    """Release DC Dispatch Proposal Lines when their Material Request is removed.

    The Proposal Line -> Material Request link is operational traceability only.
    It must not create a reverse dependency that blocks normal Material Request
    cancellation/deletion. ERPNext's own downstream dependency checks remain
    untouched, so a real downstream document such as a Stock Entry can still
    prevent cancellation/deletion when standard ERPNext rules require it.
    """
    material_request = getattr(doc, "name", None)
    if not material_request:
        return

    frappe.db.sql(
        """
        UPDATE `tabDC Dispatch Proposal Line`
        SET material_request = NULL
        WHERE material_request = %(material_request)s
        """,
        {"material_request": material_request},
    )