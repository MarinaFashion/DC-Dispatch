from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import flt


def validate_growth_rows(run):
    for row in run.store_rules:
        growth = flt(
            getattr(
                row,
                "expected_growth",
                0,
            )
        )

        if growth < -100:
            frappe.throw(
                _(
                    "Expected Growth % cannot be below -100% "
                    "for store {0}."
                ).format(
                    row.store_warehouse
                )
            )


def growth_multiplier(rule):
    growth = flt(
        getattr(
            rule,
            "expected_growth",
            0,
        )
    )
    return max(
        0.0,
        1.0 + growth / 100.0,
    )


def final_demand(
    net_demand,
    expected_growth,
):
    return max(
        0.0,
        flt(net_demand)
        * (
            1.0
            + flt(expected_growth)
            / 100.0
        ),
    )


def recalculate_final_demands(run):
    """Calculate the displayed Final Demand from the row's current Net Demand."""
    validate_growth_rows(run)

    for row in run.store_rules:
        row.final_demand = final_demand(
            getattr(
                row,
                "historical_demand_qty",
                0,
            ),
            getattr(
                row,
                "expected_growth",
                0,
            ),
        )


def apply_growth_to_scores(
    run,
    scores,
):
    """Apply each destination store's forecast growth to cohort demand scores."""
    rules = {
        row.store_warehouse: row
        for row in run.store_rules
        if row.decision != "Exclude"
    }

    return {
        warehouse: max(
            0.0,
            flt(score)
            * growth_multiplier(
                rules[warehouse]
            ),
        )
        for warehouse, score in scores.items()
        if warehouse in rules
    }


def configuration_signature(run):
    payload = [
        [
            row.store_warehouse,
            flt(
                getattr(
                    row,
                    "expected_growth",
                    0,
                )
            ),
        ]
        for row in sorted(
            run.store_rules,
            key=lambda value: (
                value.store_warehouse or ""
            ),
        )
    ]

    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def assert_forecast_configuration_unchanged(
    run,
):
    stored = str(
        getattr(
            run,
            "forecast_growth_signature",
            "",
        )
        or ""
    )
    current = configuration_signature(
        run
    )

    # Backward compatibility with proposals created before v0.6.3.
    if not stored:
        if all(
            flt(
                getattr(
                    row,
                    "expected_growth",
                    0,
                )
            )
            == 0
            for row in run.store_rules
        ):
            return

    if stored != current:
        frappe.throw(
            _(
                "Expected Growth % changed after calculation. "
                "Recalculate the proposal before continuing."
            )
        )
