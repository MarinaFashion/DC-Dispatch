from __future__ import annotations

from dataclasses import dataclass, field
from math import floor
from typing import Iterable


TIER_ORDER = {"A": 0, "B": 1, "C": 2}


@dataclass(frozen=True)
class StoreInput:
    warehouse: str
    score: float
    tier: str = "B"
    priority: int = 0
    minimum_per_variant: int = 1
    # Fieldname retained for backward compatibility with existing DocType data.
    # In v0.5.0 this is a per-variant/size maximum, not a style-total maximum.
    maximum_per_style: int = 0


@dataclass
class StyleAllocation:
    quantities: dict[str, dict[str, int]]
    variant_targets: dict[str, int]
    unallocated: dict[str, int]
    skipped_stores: list[str]
    depth_targets: dict[str, int] = field(default_factory=dict)


def allocate_integer_with_caps(
    total: int,
    weights: dict[str, float],
    caps: dict[str, int],
) -> dict[str, int]:
    """Allocate whole units proportionally and redistribute quantities blocked by caps."""
    total = max(0, int(total))
    result = {key: 0 for key in caps}
    remaining = min(total, sum(max(0, int(cap)) for cap in caps.values()))

    while remaining:
        active = [
            key
            for key, cap in caps.items()
            if result[key] < max(0, int(cap)) and float(weights.get(key, 0)) > 0
        ]
        if not active:
            break

        weight_total = sum(float(weights[key]) for key in active)
        quotas = {
            key: remaining * float(weights[key]) / weight_total
            for key in active
        }

        assigned = 0
        for key in active:
            room = max(0, int(caps[key]) - result[key])
            quantity = min(room, floor(quotas[key]))
            result[key] += quantity
            assigned += quantity

        remaining -= assigned
        if not remaining:
            break

        ranked = sorted(
            active,
            key=lambda key: (
                quotas[key] - floor(quotas[key]),
                float(weights[key]),
                key,
            ),
            reverse=True,
        )
        extra_assigned = 0
        for key in ranked:
            if not remaining:
                break
            if result[key] < max(0, int(caps[key])):
                result[key] += 1
                remaining -= 1
                extra_assigned += 1

        if not assigned and not extra_assigned:
            break

    return result


def variant_dispatch_targets(
    variant_stock: dict[str, float],
    target_total: int,
) -> dict[str, int]:
    """Split a whole style budget across sizes using the current DC size ratio."""
    stock_caps = {
        item: max(0, floor(quantity))
        for item, quantity in variant_stock.items()
    }
    weights = {
        item: max(0.0, float(quantity))
        for item, quantity in variant_stock.items()
    }
    return allocate_integer_with_caps(
        min(int(target_total), sum(stock_caps.values())),
        weights,
        stock_caps,
    )


def _minimum_bundle_order(store: StoreInput):
    return (
        TIER_ORDER.get(store.tier, 99),
        int(store.priority or 0),
        -float(store.score or 0),
        store.warehouse,
    )


def _depth_order(store: StoreInput):
    return (
        TIER_ORDER.get(store.tier, 99),
        -float(store.score or 0),
        int(store.priority or 0),
        store.warehouse,
    )


def _variant_room(
    store: StoreInput,
    quantities: dict[str, dict[str, int]],
    remaining_variant_budget: dict[str, int],
) -> dict[str, int]:
    maximum = max(0, int(store.maximum_per_style))
    if maximum == 0:
        return dict(remaining_variant_budget)

    return {
        variant: min(
            remaining_variant_budget[variant],
            max(0, maximum - quantities[store.warehouse][variant]),
        )
        for variant in remaining_variant_budget
    }


def _variant_depth_additions(
    remaining_variant_budget: dict[str, int],
    room_by_variant: dict[str, int],
    budget: int,
) -> dict[str, int]:
    """Allocate a store depth budget using remaining DC size ratio, capped per size."""
    weights = {
        variant: max(0.0, float(quantity))
        for variant, quantity in remaining_variant_budget.items()
    }
    caps = {
        variant: max(0, int(room_by_variant.get(variant, 0)))
        for variant in remaining_variant_budget
    }
    return allocate_integer_with_caps(
        min(max(0, int(budget)), sum(caps.values())),
        weights,
        caps,
    )


def allocate_style(
    variant_stock: dict[str, float],
    target_total: int,
    stores: Iterable[StoreInput],
    allowed_stores: set[str] | None = None,
) -> StyleAllocation:
    """Allocate one single-color template across stores; variants are sizes only.

    v0.5.0 rules:
    1. Minimum display quantity applies to every available size.
    2. Maximum quantity applies independently to every size (0 = unlimited).
    3. Tier/priority controls minimum-bundle order.
    4. Remaining depth follows historical demand and is redistributed when a
       store/size reaches its maximum.
    """
    stores = [
        store
        for store in stores
        if allowed_stores is None or store.warehouse in allowed_stores
    ]
    bundle_order = sorted(stores, key=_minimum_bundle_order)

    physical_stock = {
        variant: max(0, floor(quantity))
        for variant, quantity in variant_stock.items()
    }
    remaining_stock = dict(physical_stock)
    remaining_total = min(
        max(0, int(target_total)),
        sum(physical_stock.values()),
    )
    quantities = {
        store.warehouse: {variant: 0 for variant in physical_stock}
        for store in stores
    }
    skipped: list[str] = []
    remainder_eligible: set[str] = set()

    # Phase 1: establish complete minimum size bundles.
    for store in bundle_order:
        minimum = max(0, int(store.minimum_per_variant))
        bundle_total = minimum * len(physical_stock)
        maximum = max(0, int(store.maximum_per_style))

        if minimum == 0:
            remainder_eligible.add(store.warehouse)
            continue

        can_cover = (
            remaining_total >= bundle_total
            and all(
                remaining_stock[variant] >= minimum
                for variant in physical_stock
            )
        )
        # Maximum is per size: every size must be allowed to reach minimum.
        cap_allows = maximum == 0 or maximum >= minimum

        if not can_cover or not cap_allows:
            skipped.append(store.warehouse)
            continue

        for variant in physical_stock:
            quantities[store.warehouse][variant] = minimum
            remaining_stock[variant] -= minimum
            remaining_total -= minimum

        remainder_eligible.add(store.warehouse)

    remaining_variant_budget = variant_dispatch_targets(
        remaining_stock,
        remaining_total,
    )
    variant_targets = {
        variant: (
            sum(
                quantities[store.warehouse][variant]
                for store in stores
            )
            + remaining_variant_budget[variant]
        )
        for variant in physical_stock
    }

    depth_candidates = [
        store
        for store in stores
        if (
            store.warehouse in remainder_eligible
            and float(store.score or 0) > 0
        )
    ]
    depth_targets = {store.warehouse: 0 for store in depth_candidates}

    # Allocate in rounds. If a store hits a per-size cap, the unused depth is
    # reconsidered for other stores rather than silently remaining stranded.
    while sum(remaining_variant_budget.values()) > 0:
        depth_caps = {}
        depth_weights = {}
        for store in depth_candidates:
            room_by_variant = _variant_room(
                store,
                quantities,
                remaining_variant_budget,
            )
            room = sum(room_by_variant.values())
            if room > 0:
                depth_caps[store.warehouse] = room
                depth_weights[store.warehouse] = max(0.0, float(store.score))

        if not depth_caps:
            break

        round_total = sum(remaining_variant_budget.values())
        round_targets = allocate_integer_with_caps(
            round_total,
            depth_weights,
            depth_caps,
        )

        progress = 0
        for store in sorted(depth_candidates, key=_depth_order):
            budget = int(round_targets.get(store.warehouse, 0))
            if budget <= 0:
                continue

            room_by_variant = _variant_room(
                store,
                quantities,
                remaining_variant_budget,
            )
            additions = _variant_depth_additions(
                remaining_variant_budget,
                room_by_variant,
                budget,
            )
            actual = 0
            for variant, quantity in additions.items():
                if quantity <= 0:
                    continue
                quantities[store.warehouse][variant] += quantity
                remaining_variant_budget[variant] -= quantity
                actual += quantity

            depth_targets[store.warehouse] += actual
            progress += actual

        if progress <= 0:
            break

    return StyleAllocation(
        quantities=quantities,
        variant_targets=variant_targets,
        unallocated=remaining_variant_budget,
        skipped_stores=skipped,
        depth_targets=depth_targets,
    )


def choose_related_set_stores(
    member_variant_stock: dict[str, dict[str, float]],
    member_target_totals: dict[str, int],
    stores: Iterable[StoreInput],
) -> set[str]:
    """Choose common stores able to receive the complete minimum bundle for every set member."""
    stores = sorted(
        stores,
        key=lambda store: (
            TIER_ORDER.get(store.tier, 99),
            store.priority,
            -float(store.score),
            store.warehouse,
        ),
    )
    remaining_stock = {
        member: {
            variant: max(0, floor(quantity))
            for variant, quantity in stock.items()
        }
        for member, stock in member_variant_stock.items()
    }
    remaining_total = {
        member: min(
            max(0, int(member_target_totals[member])),
            sum(stock.values()),
        )
        for member, stock in remaining_stock.items()
    }

    selected: set[str] = set()
    for store in stores:
        minimum = max(0, int(store.minimum_per_variant))
        if minimum == 0:
            selected.add(store.warehouse)
            continue

        cap = max(0, int(store.maximum_per_style))
        feasible = True
        for member, stock in remaining_stock.items():
            bundle_total = minimum * len(stock)
            if (
                (cap and cap < minimum)
                or remaining_total[member] < bundle_total
                or any(quantity < minimum for quantity in stock.values())
            ):
                feasible = False
                break

        if not feasible:
            continue

        selected.add(store.warehouse)
        for member, stock in remaining_stock.items():
            for variant in stock:
                stock[variant] -= minimum
                remaining_total[member] -= minimum

    return selected


def validate_related_sets(
    rows: Iterable[dict],
    expected_members: dict[str, set[str]],
) -> list[str]:
    """Return all all-or-none violations by related set and store."""
    presence: dict[tuple[str, str], set[str]] = {}

    for row in rows:
        related_set = row.get("related_set")
        if (
            not related_set
            or int(row.get("final_qty") or 0) <= 0
            or row.get("exclude")
        ):
            continue

        key = (related_set, row["store_warehouse"])
        presence.setdefault(key, set()).add(row["item_template"])

    errors = []
    for related_set, members in expected_members.items():
        stores = {
            store
            for set_name, store in presence
            if set_name == related_set
        }
        for store in stores:
            present = presence.get((related_set, store), set())
            if present != members:
                missing = ", ".join(sorted(members - present))
                errors.append(
                    f"Related Set {related_set} is incomplete for "
                    f"{store}; missing {missing}."
                )

    return errors
