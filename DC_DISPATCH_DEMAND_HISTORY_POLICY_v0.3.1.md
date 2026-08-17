# DC Dispatch v0.3.1 — Demand History Return Policy

## Commercial rule

Historical demand is now calculated as:

    Demand Units = Gross Sales - Same-Store Returns

### Return treatment
- Same-store linked return:
  deducted from that store's demand.
- Cross-store linked return:
  not deducted from the original selling store and not deducted from the
  receiving store's demand.
- Unlinked return:
  excluded from demand because the original selling store cannot be proven.

This prevents a store from receiving a lower future dispatch because merchandise
was physically returned to a different store.

## Historical Evidence audit workbook

The `Proposal -> Export Historical Evidence` workbook now exposes, as separate
numeric columns in `Sales by Store`:

- Gross Sales
- Same-Store Returns
- Cross-Store Returns Received
- Unlinked Returns Received
- Demand Units

This allows direct verification of:

    Demand Units = Gross Sales - Same-Store Returns

The workbook also includes a `Return Audit` sheet with:
- return Sales Invoice
- posting date
- Item Template
- Item Code
- return store
- original Sales Invoice
- original selling warehouse
- return quantity
- classification / demand treatment

## Important after deployment

This release changes the commercial scoring policy. Any run calculated under the
old return logic should be recalculated before approval if it will be used for
real dispatch decisions.

## Cumulative package

Includes:
- v0.2.3 direct UI action-button fixes
- v0.3.0 Historical Evidence export
- v0.3.1 demand-history return policy

## Schema

No DocType/schema change is included.
