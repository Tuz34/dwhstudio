# PRODUCTS ODS Initial Target Creation Contract

This is a static, non-executable contract for the first creation of `DWH.ODS_SH_PRODUCTS`. It does not authorize a
database change or claim live proof.

## Reviewed input

The read-only preflight established that `SH.PRODUCTS` has the reviewed 22 source columns, no duplicate natural-key
groups, and that the ODS target is absent. Evidence remains count/status/metadata only.

## Target metadata

- The target mirrors all 22 source columns without preserving history in ODS.
- `PROD_ID` is required and enforces a single current row for each product.
- `ODS_CREATED_AT` is a required insert audit timestamp.
- `ODS_UPDATED_AT` is a nullable change audit timestamp.
- Type 2 effective dating, current flags, and row-version surrogate keys remain owned by `DWH.DIM_PRODUCT`.

## Initial load and rerun gates

The first load must reconcile source, inserted, and target counts and report the duplicate natural-key group count.
Repeating the same synchronized input must become a no-op. Evidence must remain count/status/metadata only, with no raw
rows and no PK values.

## Absent-target rollback

The named absent-target baseline records that no previous target object exists. A failed first proof may return to that
state only when the target was created by the approved run and the downstream dimension was not promoted. Cleanup is a
separate persistent action and requires its own exact operator approval. Live restore has not been tested.

## Approval boundary

The initial target action requires all of the following:

1. Independent review of the read-only preflight and this contract.
2. A reviewed, separate executor implementation.
3. A separately approved disposable cleanup/restore test.
4. Exact operator approval for the initial target action.

This document contains no executable statements, connection material, raw data, or key values.

Safety evidence rules:

- count/status/metadata only.
- no raw rows.
- no PK values.
