# PRODUCTS ODS Type 1 Static Contract

This metadata-only contract freezes the selected product pipeline. It does not authorize live work.

## Layer order

1. `SH.PRODUCTS -> DWH.ODS_SH_PRODUCTS`: Type 1 current state.
2. `DWH.ODS_SH_PRODUCTS -> DWH.DIM_PRODUCT`: Type 2 historical row versions.

ODS refreshes changed source attributes in place using `PROD_ID` as the natural/update key. It holds one current row
per product key and does not create history versions. Effective dates, current flags, and a distinct surrogate key per
version belong to `DIM_PRODUCT`.

## Type 1 contract

- The complete reviewed PRODUCTS metadata is required; drift fails closed.
- Every non-key source column is a current-state tracked attribute.
- Missing natural keys are insert candidates; existing keys are refresh candidates.
- Repeating the same input is a no-op and cannot add a duplicate key.
- Evidence contains insert, refresh, unchanged, duplicate, source, and target counts/status only.

## Backup and promotion gate

The current ODS target and restore path must be preserved before persistent action. The downstream DIM_PRODUCT step
remains blocked until ODS Type 1 has reviewed metadata/count preflight, rerun evidence, DQ evidence, and exact approval.

## Safety boundary

- no raw rows; no PK values; no secrets or connection material.
- no executable statements, connection, ODI repository write, scenario run, or load.
- no persistent action without a named backup, tested restore path, and exact operator approval.
