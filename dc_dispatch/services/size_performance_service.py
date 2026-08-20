from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from math import floor

import frappe
from frappe import _
from frappe.utils import cint, flt

from dc_dispatch.services.allocation import (
    StyleAllocation,
    TIER_ORDER,
    allocate_integer_with_caps,
    allocate_style,
)
from dc_dispatch.services import history_policy_service as history
from dc_dispatch.services import size_service


GROUPS = ("Small", "Medium", "Large")


def validate_size_factor_inputs(run):
    weight = flt(
        getattr(run, "size_performance_weight", 0)
    )
    enabled = cint(
        getattr(run, "include_size_performance_factor", 0)
    )

    if weight < 0 or weight > 100:
        frappe.throw(
            _("Size Performance Weight % must be between 0 and 100.")
        )

    if enabled and weight <= 0:
        frappe.throw(
            _(
                "Enter a Size Performance Weight greater than 0, "
                "or clear Include Size Performance Factor."
            )
        )

    if enabled:
        settings = frappe.get_single("DC Dispatch Settings")
        errors = size_service.validate_size_group_configuration(
            settings
        )
        if errors:
            frappe.throw("<br>".join(errors[:20]))


def configuration_signature(run):
    enabled = bool(
        cint(
            getattr(
                run,
                "include_size_performance_factor",
                0,
            )
        )
    )

    if not enabled:
        payload = {"enabled": False}
    else:
        settings = frappe.get_single(
            "DC Dispatch Settings"
        )
        groups = size_service.size_group_configuration(
            settings
        )
        payload = {
            "enabled": True,
            "weight": flt(
                getattr(
                    run,
                    "size_performance_weight",
                    0,
                )
            ),
            "size_attribute": (
                size_service.size_attribute_name(settings)
            ),
            "groups": {
                group: sorted(values)
                for group, values in groups.items()
            },
        }

    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def assert_size_configuration_unchanged(run):
    current = configuration_signature(run)
    stored = str(
        getattr(
            run,
            "size_performance_signature",
            "",
        )
        or ""
    )

    # Backward compatibility for proposals calculated before v0.6.0
    # when the size factor is disabled.
    if (
        not stored
        and not cint(
            getattr(
                run,
                "include_size_performance_factor",
                0,
            )
        )
    ):
        return

    if not stored or stored != current:
        frappe.throw(
            _(
                "Size Performance settings changed after calculation. "
                "Recalculate the proposal before continuing."
            )
        )


def _gross_size_sales_rows(run, stores):
    if not stores:
        return []

    return frappe.db.sql(
        """
        SELECT
            COALESCE(NULLIF(item.variant_of, ''), item.name)
                AS item_template,
            sii.item_code,
            sii.warehouse AS store_warehouse,
            SUM(sii.qty) AS gross_sales
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si
            ON si.name = sii.parent
           AND si.docstatus = 1
        INNER JOIN `tabItem` item
            ON item.name = sii.item_code
        WHERE si.company = %(company)s
          AND si.is_return = 0
          AND si.posting_date
              BETWEEN %(from_date)s AND %(to_date)s
          AND sii.warehouse IN %(stores)s
        GROUP BY
            COALESCE(NULLIF(item.variant_of, ''), item.name),
            sii.item_code,
            sii.warehouse
        """,
        {
            "company": run.company,
            "from_date": run.sales_from_date,
            "to_date": run.sales_to_date,
            "stores": tuple(stores),
        },
        as_dict=True,
    )


def size_demand_breakdown(run, stores):
    """Gross Sales - Same-Store Returns at variant/store level.

    This follows the same return policy as the main historical demand
    calculation. Cross-store and unresolved returns do not reduce demand.
    """
    result = {}

    for row in _gross_size_sales_rows(
        run,
        stores,
    ):
        key = (
            row.item_template,
            row.item_code,
            row.store_warehouse,
        )
        result[key] = flt(
            row.gross_sales
        )

    store_set = set(stores)
    for row in history._return_rows(
        run,
        stores,
    ):
        if (
            row.return_classification
            != "Same-Store Return - Deducted"
        ):
            continue

        store = row.return_store_warehouse
        if store not in store_set:
            continue

        key = (
            row.item_template,
            row.item_code,
            store,
        )
        result[key] = (
            flt(result.get(key, 0))
            - flt(row.return_qty)
        )

    return result


def build_size_context(
    run,
    stores,
    target_item_codes,
):
    validate_size_factor_inputs(run)

    if not cint(
        getattr(
            run,
            "include_size_performance_factor",
            0,
        )
    ):
        return None

    settings = frappe.get_single(
        "DC Dispatch Settings"
    )

    demand = size_demand_breakdown(
        run,
        stores,
    )

    historical_item_codes = {
        item_code
        for _template, item_code, _store
        in demand
    }
    all_item_codes = (
        historical_item_codes
        | set(target_item_codes or [])
    )

    group_by_item = (
        size_service.variant_size_group_map(
            all_item_codes,
            settings=settings,
        )
    )

    return {
        "demand": demand,
        "group_by_item": group_by_item,
        "stores": list(stores),
        "settings": settings,
    }


def profile_for_cohort(
    run,
    cohort_templates,
    context,
):
    if not context:
        return None

    cohort_templates = set(
        cohort_templates or []
    )
    if not cohort_templates:
        return None

    demand_by_store_group = defaultdict(
        float
    )
    group_totals = defaultdict(float)
    store_totals = defaultdict(float)

    for (
        template,
        item_code,
        store,
    ), quantity in context["demand"].items():
        if template not in cohort_templates:
            continue

        group = context[
            "group_by_item"
        ].get(item_code)
        if not group:
            continue

        quantity = max(
            0.0,
            flt(quantity),
        )
        if quantity <= 0:
            continue

        demand_by_store_group[
            (store, group)
        ] += quantity
        group_totals[group] += quantity
        store_totals[store] += quantity

    mapped_units = sum(
        group_totals.values()
    )
    if mapped_units <= 0:
        return None

    indices = {}
    for store in context["stores"]:
        store_total = max(
            0.0,
            store_totals.get(store, 0),
        )
        overall_store_share = (
            store_total / mapped_units
            if mapped_units
            else 0
        )

        for group in GROUPS:
            group_total = max(
                0.0,
                group_totals.get(group, 0),
            )
            store_group = max(
                0.0,
                demand_by_store_group.get(
                    (store, group),
                    0,
                ),
            )

            if (
                group_total <= 0
                or overall_store_share <= 0
            ):
                index = 1.0
            else:
                group_store_share = (
                    store_group
                    / group_total
                )
                index = (
                    group_store_share
                    / overall_store_share
                )

            indices[
                (store, group)
            ] = max(
                0.0,
                flt(index),
            )

    # A no-history store that uses a reference store should inherit
    # that reference store's relative size behavior just as it inherits
    # the main demand score.
    for rule in run.store_rules:
        if (
            rule.decision
            != "Use Reference Store"
            or not rule.reference_store
        ):
            continue
        for group in GROUPS:
            indices[
                (
                    rule.store_warehouse,
                    group,
                )
            ] = indices.get(
                (
                    rule.reference_store,
                    group,
                ),
                1.0,
            )

    network_group_shares = {
        group: (
            max(
                0.0,
                group_totals.get(
                    group,
                    0,
                ),
            )
            / mapped_units
        )
        for group in GROUPS
    }

    return {
        "indices": indices,
        "network_group_shares": (
            network_group_shares
        ),
        "mapped_units": mapped_units,
    }


def _store_order(store):
    return (
        TIER_ORDER.get(
            store.tier,
            99,
        ),
        int(store.priority or 0),
        -float(store.score or 0),
        store.warehouse,
    )


def _fixed_minimums(
    baseline,
    stores,
    variants,
):
    fixed = {
        store.warehouse: {
            variant: 0
            for variant in variants
        }
        for store in stores
    }

    for store in stores:
        minimum = max(
            0,
            int(
                store.minimum_per_variant
                or 0
            ),
        )
        if minimum <= 0:
            continue

        baseline_row = (
            baseline.quantities.get(
                store.warehouse,
                {},
            )
        )

        if all(
            int(
                baseline_row.get(
                    variant,
                    0,
                )
            )
            >= minimum
            for variant in variants
        ):
            for variant in variants:
                fixed[
                    store.warehouse
                ][variant] = minimum

    return fixed


def _variant_room(
    store,
    quantities,
    variant,
):
    maximum = max(
        0,
        int(
            store.maximum_per_style
            or 0
        ),
    )
    if maximum == 0:
        return 10**9

    return max(
        0,
        maximum
        - int(
            quantities[
                store.warehouse
            ][variant]
        ),
    )


def _desired_variant_additions(
    remaining_stock,
    remaining_total,
    group_by_item,
    network_group_shares,
    weight,
):
    if remaining_total <= 0:
        return {
            variant: 0
            for variant in remaining_stock
        }

    physical_total = sum(
        remaining_stock.values()
    )
    if physical_total <= 0:
        return {
            variant: 0
            for variant in remaining_stock
        }

    stock_by_group = defaultdict(int)
    other_variants = []

    for variant, quantity in (
        remaining_stock.items()
    ):
        group = group_by_item.get(
            variant
        )
        if group:
            stock_by_group[group] += quantity
        else:
            other_variants.append(
                variant
            )

    mapped_stock = sum(
        stock_by_group.values()
    )
    mapped_share = (
        mapped_stock / physical_total
        if physical_total
        else 0
    )

    available_groups = [
        group
        for group in GROUPS
        if stock_by_group.get(
            group,
            0,
        )
        > 0
    ]

    historical_total = sum(
        max(
            0.0,
            flt(
                network_group_shares.get(
                    group,
                    0,
                )
            ),
        )
        for group in available_groups
    )

    group_weights = {}

    for group in available_groups:
        stock_share = (
            stock_by_group[group]
            / physical_total
        )
        if historical_total > 0:
            historical_share = (
                mapped_share
                * max(
                    0.0,
                    flt(
                        network_group_shares.get(
                            group,
                            0,
                        )
                    ),
                )
                / historical_total
            )
        else:
            historical_share = (
                stock_share
            )

        group_weights[group] = (
            (1 - weight)
            * stock_share
            + weight
            * historical_share
        )

    other_stock = sum(
        remaining_stock[
            variant
        ]
        for variant in other_variants
    )
    if other_stock > 0:
        # Unmapped sizes remain neutral and follow their available
        # stock share; the size factor never guesses their behavior.
        group_weights["__OTHER__"] = (
            other_stock
            / physical_total
        )

    group_caps = {
        group: (
            other_stock
            if group == "__OTHER__"
            else stock_by_group.get(
                group,
                0,
            )
        )
        for group in group_weights
    }

    group_targets = (
        allocate_integer_with_caps(
            remaining_total,
            group_weights,
            group_caps,
        )
    )

    variant_targets = {
        variant: 0
        for variant in remaining_stock
    }

    for group, group_target in (
        group_targets.items()
    ):
        if group == "__OTHER__":
            variants = other_variants
        else:
            variants = [
                variant
                for variant in remaining_stock
                if (
                    group_by_item.get(
                        variant
                    )
                    == group
                )
            ]

        caps = {
            variant: remaining_stock[
                variant
            ]
            for variant in variants
        }
        weights = {
            variant: float(
                remaining_stock[
                    variant
                ]
            )
            for variant in variants
        }

        split = allocate_integer_with_caps(
            group_target,
            weights,
            caps,
        )
        for variant, quantity in (
            split.items()
        ):
            variant_targets[
                variant
            ] = quantity

    return variant_targets


def _preference_weight(
    profile,
    store,
    group,
    weight,
):
    if not group:
        return 1.0

    index = max(
        0.0,
        flt(
            profile["indices"].get(
                (
                    store,
                    group,
                ),
                1.0,
            )
        ),
    )
    return max(
        0.000001,
        (1 - weight)
        + weight * index,
    )


def allocate_style_with_size_performance(
    variant_stock,
    target_total,
    stores,
    allowed_stores,
    profile,
    group_by_item,
    weight_percent,
):
    """Keep store totals from the current demand allocator, then optimize sizes.

    Hard constraints:
      - actual DC stock per variant
      - store total from the existing historical-demand allocation
      - Tier minimum bundle
      - Tier maximum per variant

    Soft objective:
      - weighted relative size performance across stores
      - network size mix versus current DC size mix
    """
    stores = [
        store
        for store in stores
        if (
            allowed_stores is None
            or store.warehouse
            in allowed_stores
        )
    ]

    baseline = allocate_style(
        variant_stock,
        target_total,
        stores,
        allowed_stores=None,
    )

    weight = min(
        1.0,
        max(
            0.0,
            flt(weight_percent)
            / 100.0,
        ),
    )

    if (
        weight <= 0
        or not profile
        or not stores
    ):
        return baseline

    physical_stock = {
        variant: max(
            0,
            floor(quantity),
        )
        for variant, quantity
        in variant_stock.items()
    }
    variants = list(
        physical_stock
    )

    target_store_totals = {
        store.warehouse: sum(
            baseline.quantities.get(
                store.warehouse,
                {},
            ).values()
        )
        for store in stores
    }

    fixed = _fixed_minimums(
        baseline,
        stores,
        variants,
    )

    quantities = {
        store.warehouse: {
            variant: fixed[
                store.warehouse
            ][variant]
            for variant in variants
        }
        for store in stores
    }

    remaining_need = {
        store.warehouse: max(
            0,
            target_store_totals[
                store.warehouse
            ]
            - sum(
                quantities[
                    store.warehouse
                ].values()
            ),
        )
        for store in stores
    }

    remaining_stock = {
        variant: max(
            0,
            physical_stock[variant]
            - sum(
                quantities[
                    store.warehouse
                ][variant]
                for store in stores
            ),
        )
        for variant in variants
    }

    remaining_total = sum(
        remaining_need.values()
    )
    if remaining_total <= 0:
        return baseline

    desired_variant = (
        _desired_variant_additions(
            remaining_stock,
            remaining_total,
            group_by_item,
            profile[
                "network_group_shares"
            ],
            weight,
        )
    )

    def pressure(variant):
        group = group_by_item.get(
            variant
        )
        if not group:
            return (0.0, variant)

        stock_group = sum(
            remaining_stock[v]
            for v in variants
            if group_by_item.get(v) == group
        )
        total_stock = sum(
            remaining_stock.values()
        )
        stock_share = (
            stock_group / total_stock
            if total_stock
            else 0
        )
        hist_share = max(
            0.0,
            flt(
                profile[
                    "network_group_shares"
                ].get(group, 0)
            ),
        )
        ratio = (
            hist_share / stock_share
            if stock_share > 0
            else hist_share
        )
        return (
            ratio,
            variant,
        )

    ordered_variants = sorted(
        variants,
        key=pressure,
        reverse=True,
    )

    # Phase A: try to reach the blended desired quantity for every
    # variant, giving that size to stores with stronger relative
    # performance while respecting each store's fixed total.
    for variant in ordered_variants:
        target = min(
            remaining_stock[variant],
            max(
                0,
                int(
                    desired_variant.get(
                        variant,
                        0,
                    )
                ),
            ),
        )
        if target <= 0:
            continue

        group = group_by_item.get(
            variant
        )
        caps = {}
        weights = {}

        for store in stores:
            room = min(
                remaining_need[
                    store.warehouse
                ],
                _variant_room(
                    store,
                    quantities,
                    variant,
                ),
            )
            if room <= 0:
                continue
            caps[
                store.warehouse
            ] = room
            weights[
                store.warehouse
            ] = _preference_weight(
                profile,
                store.warehouse,
                group,
                weight,
            )

        if not caps:
            continue

        assigned = (
            allocate_integer_with_caps(
                target,
                weights,
                caps,
            )
        )

        for store_name, quantity in (
            assigned.items()
        ):
            if quantity <= 0:
                continue
            quantities[
                store_name
            ][variant] += quantity
            remaining_need[
                store_name
            ] -= quantity
            remaining_stock[
                variant
            ] -= quantity

    # Phase B: desired group totals are soft. If Tier caps made the
    # preferred mix infeasible, use any remaining physical stock to
    # complete the original store totals, still preferring the best
    # relative size/store fit.
    for variant in ordered_variants:
        available = min(
            remaining_stock[
                variant
            ],
            sum(
                remaining_need.values()
            ),
        )
        if available <= 0:
            continue

        group = group_by_item.get(
            variant
        )
        caps = {}
        weights = {}

        for store in stores:
            room = min(
                remaining_need[
                    store.warehouse
                ],
                _variant_room(
                    store,
                    quantities,
                    variant,
                ),
            )
            if room <= 0:
                continue
            caps[
                store.warehouse
            ] = room
            weights[
                store.warehouse
            ] = _preference_weight(
                profile,
                store.warehouse,
                group,
                weight,
            )

        if not caps:
            continue

        assigned = (
            allocate_integer_with_caps(
                available,
                weights,
                caps,
            )
        )

        for store_name, quantity in (
            assigned.items()
        ):
            if quantity <= 0:
                continue
            quantities[
                store_name
            ][variant] += quantity
            remaining_need[
                store_name
            ] -= quantity
            remaining_stock[
                variant
            ] -= quantity

    # The baseline allocator is already known to be feasible. If the
    # size optimization cannot preserve every store total because of
    # a rare cap combination, fall back to the baseline rather than
    # return an unsafe or incomplete proposal.
    if any(
        quantity > 0
        for quantity in (
            remaining_need.values()
        )
    ):
        return baseline

    # Final defensive checks.
    for variant in variants:
        allocated = sum(
            quantities[
                store.warehouse
            ][variant]
            for store in stores
        )
        if allocated > physical_stock[
            variant
        ]:
            return baseline

    for store in stores:
        store_total = sum(
            quantities[
                store.warehouse
            ].values()
        )
        if (
            store_total
            != target_store_totals[
                store.warehouse
            ]
        ):
            return baseline

        maximum = max(
            0,
            int(
                store.maximum_per_style
                or 0
            ),
        )
        if maximum:
            if any(
                quantities[
                    store.warehouse
                ][variant]
                > maximum
                for variant in variants
            ):
                return baseline

    variant_targets = {
        variant: sum(
            quantities[
                store.warehouse
            ][variant]
            for store in stores
        )
        for variant in variants
    }

    depth_targets = {
        store.warehouse: max(
            0,
            target_store_totals[
                store.warehouse
            ]
            - sum(
                fixed[
                    store.warehouse
                ].values()
            ),
        )
        for store in stores
    }

    return StyleAllocation(
        quantities=quantities,
        variant_targets=variant_targets,
        unallocated={
            variant: 0
            for variant in variants
        },
        skipped_stores=list(
            baseline.skipped_stores
        ),
        depth_targets=depth_targets,
    )
