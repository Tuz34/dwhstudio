# Phase 1 - DIM_PRODUCT static execution-plan contract

This is a non-executable static contract for the ODI-68 Type 2 SCD planning
slice. It freezes metadata and fail-closed gates only. It does not authorize or
perform persistent work.

## Safety boundary

- count/status/metadata only.
- no raw rows.
- no PK values.
- no secrets, credentials, or connection strings.
- no live DB or ODI connection.
- no executable SQL.
- no scenario or load run.
- no persistent action without explicit operator approval in a later slice.

## Finalized source contract

Source: `DWH.ODS_SH_PRODUCTS` after the reviewed `SH.PRODUCTS -> DWH.ODS_SH_PRODUCTS` Type 1 step.

Natural key: `PROD_ID NUMBER(6)`.

The metadata snapshot is based on the local Oracle sample-schema documentation.
The planner requires the complete reviewed column set and blocks on a missing,
unexpected, duplicate, invalid, or type-changed column.

### Tracked business attributes

- `PROD_NAME VARCHAR2(50)`
- `PROD_DESC VARCHAR2(4000)`
- `PROD_SUBCATEGORY VARCHAR2(50)`
- `PROD_SUBCATEGORY_ID NUMBER`
- `PROD_SUBCATEGORY_DESC VARCHAR2(2000)`
- `PROD_CATEGORY VARCHAR2(50)`
- `PROD_CATEGORY_ID NUMBER`
- `PROD_CATEGORY_DESC VARCHAR2(2000)`
- `PROD_WEIGHT_CLASS NUMBER(3)`
- `PROD_UNIT_OF_MEASURE VARCHAR2(20)`
- `PROD_PACK_SIZE VARCHAR2(30)`
- `SUPPLIER_ID NUMBER(6)`
- `PROD_STATUS VARCHAR2(20)`
- `PROD_LIST_PRICE NUMBER(8,2)`
- `PROD_MIN_PRICE NUMBER(8,2)`
- `PROD_TOTAL VARCHAR2(13)`
- `PROD_TOTAL_ID NUMBER`

### Source metadata exclusions

These columns do not participate in target change detection:

- `PROD_ID`: natural key, compared separately.
- `PROD_SRC_ID`: source technical identifier.
- `PROD_EFF_FROM`: source-owned effective dating.
- `PROD_EFF_TO`: source-owned effective dating.
- `PROD_VALID`: source-owned validity marker.
- `ODS_CREATED_AT`: ODS audit metadata.
- `ODS_UPDATED_AT`: ODS audit metadata.

## Finalized target contract

Target: `DWH.DIM_PRODUCT`.

The target carries `PRODUCT_SK NUMBER`, the natural key, all tracked business
attributes with their reviewed source types, and these SCD2/audit columns:

- `EFFECTIVE_FROM DATE`
- `EFFECTIVE_TO DATE`
- `CURRENT_FLAG VARCHAR2(1)`. The current stock contract stores `1` for the
  open row and `0` for history, with open-ended `EFFECTIVE_TO` of
  `2400-01-01`. The older `Y`/`N` and `9999-12-31` values are the
  pre-migration contract and are not accepted by current fixed-path DQ.
- `INSERT_DATE TIMESTAMP`
- `UPDATE_DATE TIMESTAMP`
- `SOURCE_SYSTEM VARCHAR2(50)`
- `ETL_BATCH_ID NUMBER`

## Surrogate key strategy

The strategy is sequence per row version, with `DWH.SEQ_DIM_PRODUCT` as the
candidate sequence and `PRODUCT_SK` as the target key column.

The existing natural-key map pattern is not reused. That pattern assigns one
surrogate key to one natural key, while Type 2 history requires a distinct key
for every historical row version of the same natural key.

## Candidate implementation route

The reviewed candidate is `server_side_expire_then_insert_candidate`. The only
operation names exposed by this contract are:

- `expire_changed_current_versions`
- `insert_new_current_versions`

They are planning labels, not executable statements. No executor exists in this
slice.

## Metadata drift gate

Any missing, unexpected, duplicate, invalid, or type-changed source column sets
the plan status to `blocked_by_metadata_drift`. In that state the persistent
operation list is empty.

## DQ and rerun guard

Required DQ gates are effective-date order, approved current-flag values, one
current row per natural key, no open-ended historical overlap, and count/status/
metadata reconciliation.

The rerun guard remains `requires_read_only_live_preflight`. Its required
behavior is:

- identical no-change input is a no-op.
- the same detected change cannot create another current version.
- a blocked or already-applied run returns status before persistent work.
- DQ failure cannot be treated as a committed/proven run.

## Approval and rollback

Even an explicit operator approval cannot move this static plan to an executor.
A separate read-only live preflight and reviewed rerun result are required first.

The rollback contract requires preservation of the previous target state, blocks
progression on DQ failure, and keeps the proven full-load path as the fallback
while SCD2 remains under review.

The next slice may add a read-only metadata/count/status preflight. Any schema
change, load behavior, ODI repository write, or executor must remain a separate
reviewed slice with explicit operator approval.
