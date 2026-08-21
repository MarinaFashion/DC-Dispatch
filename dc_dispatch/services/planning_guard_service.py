from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint

from dc_dispatch.services import background_service
from dc_dispatch.services import tier_service


PROPOSAL_STATUSES = {
    "Calculated",
    "Proposal Imported",
    "Approved",
    "Material Requests Created",
}


def _tier_rules_by_name(doc):
    return {
        str(row.tier or "").strip(): row
        for row in (doc.tier_rules or [])
        if str(row.tier or "").strip()
    }


def sync_store_limits_from_tier(doc, persist=False):
    """Make selected Store Tier authoritative for Min/Max Qty per Size.

    Priority ranges still assign the default Tier. The planner may override the
    Tier on a store. Once a Tier is selected, its Tier Allocation Rule defines
    the effective minimum and maximum per available size.
    """
    if not doc.store_rules:
        return

    if not doc.tier_rules:
        frappe.throw(_("Add Tier Allocation Rules before calculating the proposal."))

    rules = _tier_rules_by_name(doc)
    missing = []

    for row in doc.store_rules:
        tier = str(row.tier or "").strip()
        rule = rules.get(tier)

        if not rule:
            missing.append(
                f"{row.store_warehouse}: Tier {tier or '(blank)'}"
            )
            continue

        minimum = cint(rule.minimum_per_variant or 0)
        maximum = cint(rule.maximum_per_variant or 0)

        row.minimum_per_variant = minimum
        row.maximum_per_style = maximum

        if persist and row.name:
            frappe.db.set_value(
                "DC Dispatch Store Rule",
                row.name,
                {
                    "minimum_per_variant": minimum,
                    "maximum_per_style": maximum,
                },
                update_modified=False,
            )

    if missing:
        frappe.throw(
            _(
                "These stores use a Tier that has no Tier Allocation Rule: {0}"
            ).format(", ".join(missing[:50]))
        )


def validate_proposal_tier_rules(doc):
    """Reject any positive store/style allocation that breaks Tier Min/Max.

    A store either receives zero for the style, or every available variant in
    that style must be at least the Tier minimum. Maximum is checked per size.
    """
    if not doc.revision or doc.status not in PROPOSAL_STATUSES:
        return

    lines = frappe.get_all(
        "DC Dispatch Proposal Line",
        filters={
            "run": doc.name,
            "revision": doc.revision,
        },
        fields=[
            "store_warehouse",
            "item_template",
            "item_code",
            "final_qty",
            "exclude",
        ],
        limit_page_length=0,
    )

    if not lines:
        return

    rules_by_store = {
        row.store_warehouse: row
        for row in doc.store_rules
        if row.decision != "Exclude"
    }

    grouped = defaultdict(list)
    for line in lines:
        if line.store_warehouse not in rules_by_store:
            continue

        quantity = (
            0
            if line.exclude
            else cint(line.final_qty or 0)
        )
        grouped[
            (
                line.store_warehouse,
                line.item_template,
            )
        ].append(
            (
                line.item_code,
                quantity,
            )
        )

    errors = []

    for (store, template), values in grouped.items():
        positive = [
            quantity
            for _item_code, quantity in values
            if quantity > 0
        ]
        if not positive:
            continue

        rule = rules_by_store[store]
        minimum = max(
            0,
            cint(rule.minimum_per_variant or 0),
        )
        maximum = max(
            0,
            cint(rule.maximum_per_style or 0),
        )

        if minimum:
            below = [
                f"{item_code}={quantity}"
                for item_code, quantity in values
                if quantity < minimum
            ]
            if below:
                errors.append(
                    (
                        f"{store} / {template}: Tier {rule.tier} minimum "
                        f"is {minimum} per size, but "
                        + ", ".join(below[:10])
                    )
                )

        if maximum:
            above = [
                f"{item_code}={quantity}"
                for item_code, quantity in values
                if quantity > maximum
            ]
            if above:
                errors.append(
                    (
                        f"{store} / {template}: Tier {rule.tier} maximum "
                        f"is {maximum} per size, but "
                        + ", ".join(above[:10])
                    )
                )

    if errors:
        frappe.throw(
            _(
                "Proposal violates Tier Min/Max rules. Recalculate before "
                "approval or export.<br>{0}"
            ).format("<br>".join(errors[:50]))
        )


def validate_run(doc, method=None):
    """Run original Tier validation, then enforce the production guardrails."""
    tier_service.validate_run(doc, method)

    # tier_service may have assigned default tiers from Priority ranges.
    # Sync Min/Max from the final selected tier after that assignment.
    sync_store_limits_from_tier(doc, persist=False)

    # Any saved calculated/imported/finalized proposal must remain compliant.
    validate_proposal_tier_rules(doc)


@frappe.whitelist()
def start_proposal_calculation(run_name):
    """Persist effective Tier Min/Max before the background worker starts.

    This also repairs older demo runs whose Store Tier was correct but whose
    hidden Store Min/Max values were still 1 / unlimited.
    """
    run = frappe.get_doc(
        "DC Dispatch Run",
        run_name,
    )
    run.check_permission("write")

    # Do not call run.save() here: an older calculated proposal may already be
    # invalid. Persist only the effective store limits so the recalculation can
    # start and replace that proposal safely.
    sync_store_limits_from_tier(
        run,
        persist=True,
    )

    return background_service.start_proposal_calculation(
        run_name
    )
