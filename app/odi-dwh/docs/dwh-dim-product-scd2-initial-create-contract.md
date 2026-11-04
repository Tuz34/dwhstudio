# DIM_PRODUCT initial target and sequence contract

This is the static ODI-68 contract for the missing `DWH.DIM_PRODUCT` target and
the missing `DWH.SEQ_DIM_PRODUCT` sequence. It follows the live read-only
preflight from `DWH.ODS_SH_PRODUCTS` and does not expose an executor.

## Safety boundary

- count/status/metadata only.
- no raw rows.
- no PK values.
- no secrets, credentials, or connection material.
- no executable SQL.
- no DB or ODI connection.
- no persistent action.
- live DDL has not been executed.
- independent review and exact operator approval are mandatory before a later
  persistent slice.

## Reviewed live baseline

The read-only preflight reports an aligned 24-column ODS source, 72 source rows,
and zero duplicate natural-key groups. The DIM target and its candidate sequence
are absent. This is an absent-target baseline, not permission to change schema.

## Target contract

The target carries 26 target columns:

- `PRODUCT_SK NUMBER` as the required unique surrogate key.
- `PROD_ID NUMBER(6)` as the natural key.
- the 17 reviewed tracked product attributes.
- `EFFECTIVE_FROM`, `EFFECTIVE_TO`, and `CURRENT_FLAG` for row-version control.
- `INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM`, and `ETL_BATCH_ID` for load audit.

ODS audit columns are not copied into the dimension. The natural key is not
unique across history because each Type 2 version is a separate row. A single current row per natural key remains mandatory.

## Sequence contract

`DWH.SEQ_DIM_PRODUCT` is the candidate generator for `PRODUCT_SK`. It advances
once per row version, does not cycle, and does not reuse a surrogate key for a
later version of the same natural key.

## Rerun and rollback boundary

The future executor must recheck that both target and sequence are absent before
any persistent action. A disposable proof must validate setup and cleanup for
fixed proof objects first.

Only objects created by the same approved call may be considered for cleanup.
The ODS source must remain unchanged. Any real cleanup requires separate
approval because Oracle schema changes commit independently of caller rollback.
Live restore has not been tested.

## Required next steps

1. Independent review of this static contract and its tests.
2. A separate disposable proof executor with fixed object identities.
3. Exact operator approval for that disposable proof.
4. A separately reviewed initial setup executor.
5. A different exact operator approval for the real DIM target and sequence.

No SCD2 load, expire, insert, scenario generation, or scenario run is authorized
by this document.
