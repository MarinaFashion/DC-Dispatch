# DC Dispatch v0.4.0 — Historical Reference Filters

## What is new

This release adds **Historical Reference Filters** to Block A on **DC Dispatch Run**.

You can now restrict the historical reference pool used in demand scoring by Item
attributes such as:
- Item Year
- Season
- Collection
- Drop / Batch
- Subgroup
- or any other configured Item field

Example:
- Main Group = Dresses
- Item Year = 2026
- Season = Ramadan

This makes the module rely on **Ramadan Dresses 2026** history instead of all
Dresses sold in the selected date range.

## Commercial logic

Historical sales selection is now split into two layers:

1. **Historical Reference Filters**
   - decides which historical items are allowed into the reference pool
2. **Matching Fields by Main Group + Minimum Match %**
   - decides how similar the historical items must be to the target style

## Evidence workbook improvements

The **Historical Evidence** export now includes:
- `Historical Scope Filters` sheet
- `Target Cohorts` sheet showing the historical scope applied to each target item
- the same sales audit columns already introduced in v0.3.1:
  - Gross Sales
  - Same-Store Returns
  - Cross-Store Returns Received
  - Unlinked Returns Received
  - Demand Units

## Important deployment note

This release adds a **new child DocType** and a **new field** on `DC Dispatch Run`.
After deployment you must run **migrate**.

## Included in this cumulative package

- v0.2.3 action button fixes
- v0.3.0 Historical Evidence export
- v0.3.1 demand-history return policy
- v0.4.0 Historical Reference Filters
