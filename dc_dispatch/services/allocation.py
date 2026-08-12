from __future__ import annotations

from dataclasses import dataclass
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
    maximum_per_style: int = 0


@dataclass
class StyleAllocation:
    quantities: dict[str, dict[str, int]]
    variant_targets: dict[str, int]
    unallocated: dict[str, int]
    skipped_stores: list[str]


def allocate_integer_with_caps(total: int, weights: dict[str, float], caps: dict[str, int]) -> dict[str, int]:
    """Allocate whole units proportionally, repeatedly redistributing quantities blocked by caps."""
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
        quotas = {key: remaining * float(weights[key]) / weight_total for key in active}
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
            key=lambda key: (quotas[key] - floor(quotas[key]), float(weights[key]), key),
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


def variant_dispatch_targets(variant_stock: dict[str, float], target_total: int) -> dict[str, int]:
    stock_caps = {item: max(0, floor(quantity)) for item, quantity in variant_stock.items()}
    weights = {item: max(0.0, float(quantity)) for item, quantity in variant_stock.items()}
    return allocate_integer_with_caps(min(int(target_total), sum(stock_caps.values())), weights, stock_caps)


def allocate_style(
    variant_stock: dict[str, float],
    target_total: int,
    stores: Iterable[StoreInput],
    allowed_stores: set[str] | None = None,
) -> StyleAllocation:
    """Allocate one single-color template across stores; its variants are sizes only."""
    stores = sorted(
        [store for store in stores if allowed_stores is None or store.warehouse in allowed_stores],
        key=lambda store: (TIER_ORDER.get(store.tier, 99), store.priority, store.warehouse),
    )
    physical_stock = {variant: max(0, floor(quantity)) for variant, quantity in variant_stock.items()}
    remaining_stock = dict(physical_stock)
    remaining_total = min(max(0, int(target_total)), sum(physical_stock.values()))
    quantities = {store.warehouse: {variant: 0 for variant in physical_stock} for store in stores}
    skipped: list[str] = []
    remainder_eligible: set[str] = set()

    # Display bundle comes first. A store receives all available sizes or receives zero for the style.
    for store in stores:
        minimum = max(0, int(store.minimum_per_variant))
        bundle_total = minimum * len(physical_stock)
        maximum = max(0, int(store.maximum_per_style))
        if minimum == 0:
            remainder_eligible.add(store.warehouse)
            continue
        can_cover = remaining_total >= bundle_total and all(
            remaining_stock[variant] >= minimum for variant in physical_stock
        )
        cap_allows = not maximum or maximum >= bundle_total
        if not can_cover or not cap_allows:
            skipped.append(store.warehouse)
            continue
        for variant in physical_stock:
            quantities[store.warehouse][variant] = minimum
            remaining_stock[variant] -= minimum
            remaining_total -= minimum
        remainder_eligible.add(store.warehouse)

    # The display bundles take priority. The balance then follows the remaining DC size ratio.
    remaining = variant_dispatch_targets(remaining_stock, remaining_total)
    targets = {
        variant: sum(quantities[store.warehouse][variant] for store in stores) + remaining[variant]
        for variant in physical_stock
    }

    # Allocate each remaining size budget using store demand, while observing the style/store cap.
    for variant in physical_stock:
        caps = {}
        weights = {}
        for store in stores:
            if store.warehouse not in remainder_eligible or store.score <= 0:
                continue
            already = sum(quantities[store.warehouse].values())
            maximum = max(0, int(store.maximum_per_style))
            room = remaining[variant] if maximum == 0 else max(0, maximum - already)
            caps[store.warehouse] = room
            weights[store.warehouse] = max(0.0, float(store.score))
        additions = allocate_integer_with_caps(remaining[variant], weights, caps)
        for warehouse, quantity in additions.items():
            quantities[warehouse][variant] += quantity
            remaining[variant] -= quantity

    return StyleAllocation(
        quantities=quantities,
        variant_targets=targets,
        unallocated=remaining,
        skipped_stores=skipped,
    )


def choose_related_set_stores(
    member_variant_stock: dict[str, dict[str, float]],
    member_target_totals: dict[str, int],
    stores: Iterable[StoreInput],
) -> set[str]:
    """Choose common stores that can receive the complete minimum bundle for every set member."""
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
        member: {variant: max(0, floor(quantity)) for variant, quantity in stock.items()}
        for member, stock in member_variant_stock.items()
    }
    remaining_total = {
        member: min(max(0, int(member_target_totals[member])), sum(stock.values()))
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
                (cap and cap < bundle_total)
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
        if not related_set or int(row.get("final_qty") or 0) <= 0 or row.get("exclude"):
            continue
        key = (related_set, row["store_warehouse"])
        presence.setdefault(key, set()).add(row["item_template"])

    errors = []
    for related_set, members in expected_members.items():
        stores = {store for set_name, store in presence if set_name == related_set}
        for store in stores:
            present = presence.get((related_set, store), set())
            if present != members:
                missing = ", ".join(sorted(members - present))
                errors.append(f"Related Set {related_set} is incomplete for {store}; missing {missing}.")
    return errors
