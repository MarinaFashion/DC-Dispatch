from __future__ import annotations

import frappe


DEFAULT_SIZE_ATTRIBUTE = "Size"
DEFAULT_GROUPS = {
    "Small": "XS,S",
    "Medium": "M",
    "Large": "L,XL,XXL,XXXL",
}


def _normal(value):
    return str(value or "").strip().upper()


def _split(value):
    return {
        _normal(part)
        for part in str(value or "").replace(";", ",").split(",")
        if _normal(part)
    }


def size_attribute_name(settings=None):
    settings = settings or frappe.get_single("DC Dispatch Settings")
    return str(
        getattr(settings, "size_attribute_name", None)
        or DEFAULT_SIZE_ATTRIBUTE
    ).strip()


def size_group_configuration(settings=None):
    settings = settings or frappe.get_single("DC Dispatch Settings")

    result = {
        "Small": _split(
            getattr(settings, "small_size_abbreviations", None)
            or DEFAULT_GROUPS["Small"]
        ),
        "Medium": _split(
            getattr(settings, "medium_size_abbreviations", None)
            or DEFAULT_GROUPS["Medium"]
        ),
        "Large": _split(
            getattr(settings, "large_size_abbreviations", None)
            or DEFAULT_GROUPS["Large"]
        ),
    }
    return result


def validate_size_group_configuration(settings=None):
    groups = size_group_configuration(settings)
    errors = []

    for name, values in groups.items():
        if not values:
            errors.append(f"{name} Size Group has no configured abbreviations.")

    seen = {}
    for group, values in groups.items():
        for value in values:
            if value in seen:
                errors.append(
                    f"Size abbreviation {value} is assigned to both "
                    f"{seen[value]} and {group}."
                )
            seen[value] = group

    return errors


def item_attribute_abbreviation_map(attribute_name=None):
    attribute_name = attribute_name or size_attribute_name()

    rows = frappe.get_all(
        "Item Attribute Value",
        filters={"parent": attribute_name},
        fields=["attribute_value", "abbr"],
        order_by="idx asc",
        limit_page_length=0,
    )

    result = {}
    for row in rows:
        value = str(row.attribute_value or "").strip()
        if not value:
            continue
        result[_normal(value)] = str(row.abbr or value).strip()

    return result


def variant_size_info(item_codes, attribute_name=None):
    item_codes = sorted(
        {
            str(item_code).strip()
            for item_code in item_codes
            if item_code
        }
    )
    if not item_codes:
        return {}

    attribute_name = attribute_name or size_attribute_name()
    abbreviations = item_attribute_abbreviation_map(attribute_name)

    rows = frappe.get_all(
        "Item Variant Attribute",
        filters={
            "parent": ["in", item_codes],
            "attribute": attribute_name,
        },
        fields=["parent", "attribute_value"],
        limit_page_length=0,
    )

    result = {}
    for row in rows:
        value = str(row.attribute_value or "").strip()
        if not value:
            continue
        result[row.parent] = {
            "attribute_value": value,
            "abbreviation": abbreviations.get(
                _normal(value),
                value,
            ),
        }

    return result


def variant_size_display_map(item_codes, attribute_name=None):
    info = variant_size_info(
        item_codes,
        attribute_name=attribute_name,
    )
    return {
        item_code: values["abbreviation"]
        for item_code, values in info.items()
    }


def variant_size_group_map(
    item_codes,
    settings=None,
    attribute_name=None,
):
    settings = settings or frappe.get_single("DC Dispatch Settings")
    attribute_name = attribute_name or size_attribute_name(settings)
    groups = size_group_configuration(settings)
    info = variant_size_info(
        item_codes,
        attribute_name=attribute_name,
    )

    abbreviation_to_group = {}
    for group, abbreviations in groups.items():
        for abbreviation in abbreviations:
            abbreviation_to_group[_normal(abbreviation)] = group

    result = {}
    for item_code, values in info.items():
        abbreviation = values["abbreviation"]
        group = abbreviation_to_group.get(
            _normal(abbreviation)
        )
        if group:
            result[item_code] = group

    return result
