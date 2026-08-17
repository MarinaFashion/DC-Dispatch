from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from dc_dispatch.services import history_policy_service as history
from dc_dispatch.services import run_service as rs
from dc_dispatch.services import tier_service


@frappe.whitelist()
def arrange_stores_by_net_demand(run_name):
    """Calculate current historical Net Demand, rank stores, and reapply Tier rules."""
    run = frappe.get_doc("DC Dispatch Run", run_name)
    rs._require_editable(run)
    rs._require_saved(run)

    if not run.store_rules:
        frappe.throw(_("Load eligible stores first."))

    # Use the existing validated history policy so ranking exactly matches
    # DC Dispatch demand: Gross Sales - Same-Store Returns, with the same
    # historical scope filters used by Check Store History.
    result = history.analyze_store_history(run)

    demand_by_store = {
        row["store"]: max(
            0,
            flt(row.get("demand_units") or row.get("net_units") or 0),
        )
        for row in result.get("stores", [])
    }

    old_priority = {
        row.store_warehouse: int(row.priority or 0)
        for row in run.store_rules
    }

    for row in run.store_rules:
        row.historical_demand_qty = demand_by_store.get(
            row.store_warehouse, 0
        )

    # Highest Net Demand first. For equal demand, retain the existing priority
    # where possible, then warehouse name for deterministic ordering.
    ordered = sorted(
        list(run.store_rules),
        key=lambda row: (
            -flt(row.historical_demand_qty),
            (
                old_priority.get(row.store_warehouse, 0)
                if old_priority.get(row.store_warehouse, 0) > 0
                else 10**9
            ),
            row.store_warehouse,
        ),
    )

    run.store_rules = ordered
    for priority, row in enumerate(run.store_rules, start=1):
        row.idx = priority
        row.priority = priority

    # Priority changed, so Tier/Min/Max must follow the configured ranges.
    if run.tier_rules:
        tier_service._validate_tier_rules(run)
        tier_service._apply_rules(run, require_full_coverage=True)
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
            }
            for row in run.store_rules
        ],
    }
