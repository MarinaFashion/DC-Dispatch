# DC Dispatch v0.3.0 — Historical Evidence Audit

## New permanent feature
Adds **Proposal → Export Historical Evidence** to DC Dispatch Run.

The export is available once target items and historical matching fields exist.
It can be used before proposal calculation to inspect the evidence that would be
used, or after calculation to reconcile the evidence to the proposal scores.

## Workbook sheets
1. **Run Summary**
   - sales period
   - match threshold
   - matching configuration
   - included stores
   - return-warehouse handling rule

2. **Target Cohorts**
   - one row per target style
   - selected/comparable matching fields
   - matching historical template count
   - cohort net units
   - stores with positive sales

3. **Historical Templates**
   - exact historical Item Templates included for each target style
   - actual match percentage
   - field-by-field match detail
   - historical template net units

4. **Sales by Store**
   - target style
   - historical template used
   - store warehouse
   - net units used by the calculation

5. **Score Reconciliation**
   - raw cohort score
   - reference-store substitution if used
   - applied historical demand score
   - share %
   - current proposal score, when a revision exists
   - reconciliation Yes/No

## Calculation consistency
The export calls the same historical sales aggregation used by proposal
calculation, including:
- submitted Sales Invoices only
- configured company and sales period
- only included stores
- returns deducted
- linked returns attributed to the original selling warehouse
- same Main Group gating
- same selected matching fields
- same minimum match percentage
- same reference-store score substitution

If a proposal revision already exists, the export refuses to run when the
calculation inputs have changed, so the audit cannot silently describe different
criteria from the calculated revision.

## Cumulative fix
This package also includes the v0.2.3 direct action-button fixes.

## Deployment
No DocType/schema change is included. `bench migrate` is not required specifically
for this release. Deploy/build and hard-refresh the browser after installation.
