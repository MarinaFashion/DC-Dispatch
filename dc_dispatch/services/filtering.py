from __future__ import annotations


def cascading_options(rows, selections, fieldnames):
    """Build each filter's options while applying every other selection."""
    options = {}
    for target, target_fieldname in fieldnames.items():
        if not target_fieldname:
            options[target] = []
            continue
        values = {
            row.get(target_fieldname)
            for row in rows
            if _matches_other_filters(row, target, selections, fieldnames)
            and row.get(target_fieldname) not in (None, "")
        }
        options[target] = sorted(values, key=lambda value: str(value).casefold())
    return options


def _matches_other_filters(row, target, selections, fieldnames):
    for run_fieldname, selected in selections.items():
        if run_fieldname == target or selected in (None, ""):
            continue
        item_fieldname = fieldnames.get(run_fieldname)
        if not item_fieldname:
            continue
        if row.get(item_fieldname) != selected:
            return False
    return True
