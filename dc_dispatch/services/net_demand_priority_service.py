from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from dc_dispatch.services import history_policy_service as history
from dc_dispatch.services import run_service as rs
from dc_dispatch.services import tier_service


@frappe.whitelist()
def arrange_stores_by_net_demand(run_name):
    """Rank stores by Net Demand and place reference stores directly after their source."""
    run = frappe.get_doc("DC Dispatch Run", run_name)
    rs._require_editable(run)
    rs._require_saved(run)

    if not run.store_rules:
        frappe.throw(_("Load eligible stores first."))

    result = history.analyze_store_history(run)

    own_demand = {
        row["store"]: max(
            0,
            flt(row.get("demand_units") or row.get("net_units") or 0),
        )
        for row in result.get("stores", [])
    }
    rows_by_store = {
        row.store_warehouse: row for row in run.store_rules
    }
    old_priority = {
        row.store_warehouse: int(row.priority or 0)
        for row in run.store_rules
    }

    def effective_demand(store, trail=None):
        trail = set(trail or ())
        if store in trail:
            return own_demand.get(store, 0)

        row = rows_by_store.get(store)
        if not row:
            return own_demand.get(store, 0)

        if (
            row.decision == "Use Reference Store"
            and row.reference_store
            and row.reference_store in rows_by_store
        ):
            return effective_demand(
                row.reference_store,
                trail | {store},
            )

        return own_demand.get(store, 0)

    for row in run.store_rules:
        row.historical_demand_qty = effective_demand(
            row.store_warehouse
        )

    children = defaultdict(list)
    roots = []
    excluded = []

    for row in run.store_rules:
        if row.decision == "Exclude":
            excluded.append(row)
        elif (
            row.decision == "Use Reference Store"
            and row.reference_store in rows_by_store
        ):
            children[row.reference_store].append(row)
        else:
            roots.append(row)

    def tie_key(row):
        previous = old_priority.get(row.store_warehouse, 0)
        return (
            previous if previous > 0 else 10**9,
            row.store_warehouse,
        )

    roots.sort(
        key=lambda row: (
            -flt(row.historical_demand_qty),
            *tie_key(row),
        )
    )
    for group in children.values():
        group.sort(key=tie_key)
    excluded.sort(
        key=lambda row: (
            -flt(row.historical_demand_qty),
            *tie_key(row),
        )
    )

    ordered = []
    visited = set()

    def append_with_references(row):
        if row.store_warehouse in visited:
            return
        visited.add(row.store_warehouse)
        ordered.append(row)
        for child in children.get(row.store_warehouse, []):
            append_with_references(child)

    for row in roots:
        append_with_references(row)

    # Any malformed/cyclic reference rows are kept safely rather than lost.
    leftovers = [
        row
        for row in run.store_rules
        if row.store_warehouse not in visited
        and row.decision != "Exclude"
    ]
    leftovers.sort(
        key=lambda row: (
            -flt(row.historical_demand_qty),
            *tie_key(row),
        )
    )
    for row in leftovers:
        append_with_references(row)

    # Excluded stores remain visible but are placed after active stores.
    ordered.extend(
        row for row in excluded
        if row.store_warehouse not in {
            value.store_warehouse for value in ordered
        }
    )

    run.store_rules = ordered
    for priority, row in enumerate(run.store_rules, start=1):
        row.idx = priority
        row.priority = priority

    if run.tier_rules:
        tier_service._validate_tier_rules(run)
        tier_service._apply_rules(
            run,
            require_full_coverage=True,
        )
        run.tier_defaults_applied = 1

    run.save()

    return {
        "stores": len(run.store_rules),
        "no_history": result.get("no_history", []),
        "ranking": [
            {
                "priority": row.priority,
                "store": row.store_warehouse,
                "net_demand": flt(row.historical_demand_qty),
                "tier": row.tier,
                "reference_store": row.reference_store,
            }
            for row in run.store_rules
        ],
    }
