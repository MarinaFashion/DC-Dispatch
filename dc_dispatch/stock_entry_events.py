import frappe


def preserve_dispatch_route(doc, method=None):
    """Preserve the exact DC -> target transit route from DC Dispatch MRs.

    ERPNext's normal Stock Entry creation remains untouched for every other
    Material Request.
    """
    if doc.doctype != "Stock Entry" or not doc.get("items"):
        return

    dispatch_runs = set()
    final_stores = set()
    instructions = set()
    routes = []

    mr_cache = {}
    mr_item_cache = {}

    for row in doc.items:
        mr_name = row.get("material_request")
        if not mr_name:
            continue

        if mr_name not in mr_cache:
            mr_cache[mr_name] = frappe.db.get_value(
                "Material Request",
                mr_name,
                [
                    "custom_dc_dispatch_run",
                    "custom_final_store_warehouse",
                    "custom_dc_dispatch_instructions",
                    "set_from_warehouse",
                    "set_warehouse",
                ],
                as_dict=True,
            )

        mr = mr_cache[mr_name]
        if not mr or not mr.custom_dc_dispatch_run:
            continue

        dispatch_runs.add(mr.custom_dc_dispatch_run)
        if mr.custom_final_store_warehouse:
            final_stores.add(mr.custom_final_store_warehouse)
        if mr.custom_dc_dispatch_instructions:
            instructions.add(mr.custom_dc_dispatch_instructions)

        source = None
        target = None
        mr_item_name = row.get("material_request_item")

        if mr_item_name:
            if mr_item_name not in mr_item_cache:
                mr_item_cache[mr_item_name] = frappe.db.get_value(
                    "Material Request Item",
                    mr_item_name,
                    ["from_warehouse", "warehouse"],
                    as_dict=True,
                )
            mr_item = mr_item_cache[mr_item_name]
            if mr_item:
                source = mr_item.from_warehouse
                target = mr_item.warehouse

        source = source or mr.set_from_warehouse
        target = target or mr.set_warehouse

        if source and target:
            row.s_warehouse = source
            row.t_warehouse = target
            routes.append((source, target))

    if not routes:
        return

    sources = {source for source, _target in routes}
    targets = {target for _source, target in routes}

    if len(sources) == 1:
        doc.from_warehouse = next(iter(sources))
    if len(targets) == 1:
        doc.to_warehouse = next(iter(targets))

    if (
        len(dispatch_runs) == 1
        and doc.meta.has_field("custom_dc_dispatch_run")
    ):
        doc.custom_dc_dispatch_run = next(iter(dispatch_runs))

    if (
        len(final_stores) == 1
        and doc.meta.has_field("custom_final_store_warehouse")
    ):
        doc.custom_final_store_warehouse = next(iter(final_stores))

    if (
        len(instructions) == 1
        and doc.meta.has_field("custom_dc_dispatch_instructions")
    ):
        doc.custom_dc_dispatch_instructions = next(iter(instructions))
