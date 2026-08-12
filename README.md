# DC Dispatch

DC Dispatch is a standalone ERPNext v15 app for the first dispatch of newly received fashion styles from a Distribution Center to stores. Later DC replenishment and store-to-store reallocation remain outside this app and continue through Marina's Stock Allocation app.

## Release 1 workflow

1. Create a **DC Dispatch Run**.
2. In Block A, select the historical sales period and add matching Item fields separately for each Item Main Group. Main Group values are selected from actual Item Template data rather than typed.
3. In Block B, use the mutually cascading Item Year, Season, Collection, Drop/Batch, Main Group, and Item Subgroup selectors, then load the target single-color templates. The selectors show only combinations that exist on active Item Templates.
4. If needed, add optional advanced metadata filters for criteria not covered by the controlled selectors.
5. Load eligible store Warehouses using the configured Warehouse **Is Store (used in Allocation)** field.
6. Resolve stores without history by excluding them or mapping them to a reference store.
7. Calculate the store-and-size proposal.
8. Export the protected Excel workbook, edit only Final Qty / Exclude / Override Reason, and attach it back to the run.
9. Import and validate the reviewed proposal.
10. Approve it and create one Material Request per final store, targeting its transit warehouse.

## Catalog assumptions

- One Item Template represents one style in one color.
- Variants under that template are sizes only.
- A second color is a separate Item Template with its own display date, season, collection, batch/drop, dispatch percentage, and allocation.
- Related products use a configurable existing Item field (for example `custom_related_set`). All in-stock members of the same set must be selected, and every receiving store gets all members or none.

## Configuration

Open **DC Dispatch Settings** after installation and verify the actual fieldnames on your site:

| Setting | Default |
|---|---|
| Warehouse Is Store Field | `custom_is_store` |
| Warehouse Transit Field | `custom_transit_warehouse` |
| Item Main Group Field | `custom_item_main_group` |
| Item Subgroup Field | `item_sub_group` |
| Item Related Set Field | `custom_related_set` |

The app deliberately stores fieldnames in Settings instead of hard-coding site-specific customizations.

The four common catalog filters use the same Item fieldnames as Marina's Stock Allocation app:

| Run filter | Item fieldname |
|---|---|
| Item Year | `item_year` |
| Season | `season` |
| Collection | `collection` |
| Drop / Batch | `custom_drop` |

Main Group and Item Subgroup use the configurable mappings shown above. All six controls are populated from active Item Templates in one metadata query and narrow each other automatically. The generic child table remains available only as **Advanced Item Filters (Optional)**.

Marina's verified Item fieldnames are:

- `item_year`, `season`, `collection`, `custom_drop`
- `custom_item_main_group`, `item_sub_group`
- `custom_fit`, `custom_neckline`, `custom_fabric`, `custom_occasion`
- `custom_style`, `custom_color_category_`, `custom_item_length`, `sleeve_type`
- `colour_theme`, `age_group`, `status`, and `display_date`

The latter fields are discovered from Item metadata and can be selected for historical exact matching or optional advanced filtering. Select values are compared after trimming whitespace and ignoring letter case.

If a configured optional Item field is missing, the remaining dropdowns continue to load and the form displays the exact invalid mapping. Existing v0.1.1 settings using `custom_item_sub_group` are migrated automatically to `item_sub_group` when the site is updated.

## Workspace

The public **DC Dispatch** workspace is placed under Stock and includes:

- New and existing DC Dispatch Runs
- DC Dispatch Settings
- Proposal Lines and Stock Snapshots for review and audit
- Related Material Requests
- Item and Warehouse master data

`DC Dispatch Run Item` remains an internal child table of `DC Dispatch Run`; it is not exposed as a standalone workspace document.

## Calculation rules

- Historical net sales use submitted Sales Invoices within the selected dates.
- Returns are included. When ERPNext provides the original Sales Invoice Item link, a cross-store return is attributed to the original selling warehouse.
- Historical templates must share the target Item Main Group and meet the run's minimum percentage across selected, nonblank matching fields for that group.
- A new store mapped to a reference store copies that store's demand score, then all store shares are recalculated to total 100%.
- Dispatch target equals whole current DC stock multiplied by the planner's dispatch percentage.
- The DC size ratio sets the per-size target.
- Store tiers and manual priority control minimum display bundle order.
- A store receives every in-stock size at its minimum quantity or receives zero for the entire style.
- Zero-demand stores receive their minimum display bundle only.
- Store maximums trigger repeated proportional redistribution.
- Any unallocatable quantity remains in the DC.

## Safety and audit controls

- The same template cannot be loaded into another non-cancelled initial dispatch run.
- Every proposal has a revision number and exact DC stock snapshot.
- A calculation-input fingerprint blocks export, import, approval, or execution if any run criteria changed after calculation.
- Any stock change blocks Excel import, approval, and Material Request creation until recalculation.
- Imported workbooks must contain every original proposal row and no fabricated rows.
- Edited/excluded rows require an override reason.
- Variant totals cannot exceed snapshot stock; template totals cannot exceed dispatch targets.
- Related-set all-or-none validation runs during import and approval.
- Material Request creation is idempotent per run and final store.
- The app adds three read-only traceability fields to Material Request: DC Dispatch Run, Final Store Warehouse, and DC Dispatch Instructions.

## Installation

```bash
bench get-app https://github.com/MarinaFashion/DC-Dispatch.git
bench --site your-site install-app dc_dispatch
bench --site your-site migrate
bench build --app dc_dispatch
```

Use a demo site first. Confirm the five custom-field mappings in Settings before creating a run.

## Local tests

The allocation tests are deliberately independent of a Frappe site:

```bash
PYTHONPATH=. python -m unittest discover -s dc_dispatch/tests -v
```

Full integration testing still requires a Frappe/ERPNext v15 bench with representative Item, Bin, Sales Invoice, Warehouse, and Material Request data.
