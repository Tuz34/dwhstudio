# DIM_PRODUCT stock migration runbook

This is the current fixed-path contract for `DWH.DIM_PRODUCT`. It records the
owner decision to move that table onto stock IKM literals with a retained
backup. It does not apply the migration, open a database connection, or change
the ODI repository.

## Current contract

Era: `stock_ikm_1_0_2400`.

- Current row: `CURRENT_FLAG = '1'` and `EFFECTIVE_TO = DATE '2400-01-01'`.
- Historical row: `CURRENT_FLAG = '0'` and a closed `EFFECTIVE_TO`.
- `CURRENT_FLAG` stays `VARCHAR2(1)`. `'1'` and `'0'` fit that type, so the
  data cutover is one `UPDATE`. A type change is not part of this slice.
- `EFFECTIVE_TO` stays `DATE`. Only the legacy open sentinel
  `DATE '9999-12-31'` becomes `DATE '2400-01-01'`.
- Dedicated mapping expressions already use `TO_DATE('24000101','YYYYMMDD')`
  and `CURRENT_FLAG` expression `1`. Load SQL, candidate preflight, read-only
  preflight, and the attribute-change proof use the same stock literals.
- New SQL versions write `INSERT_DATE` and `UPDATE_DATE` with `SYSTIMESTAMP`,
  `SOURCE_SYSTEM` `'SH'`, and `ETL_BATCH_ID` from the approved batch id. That
  is the SQL-path population for the same four audit columns the mapping
  expressions send into the stock IKM flow.

Stock DQ accepts only `'1'` and `'0'`. A `9999-12-31` end date, a `Y`/`N`
flag, a current row that does not end at `2400-01-01`, or a historical row
that still carries an open sentinel is invalid. Mixed rows fail closed.

## Pre-migration contract

Era: `pre_migration_yn_9999`.

- Current row: `CURRENT_FLAG = 'Y'` and `EFFECTIVE_TO = DATE '9999-12-31'`.
- Historical row: `CURRENT_FLAG = 'N'` and a closed `EFFECTIVE_TO`.

The initial-create plan still names this block
`pre_migration_current_row_contract`. Load, candidate, and read-only preflight
do not accept it. The migration classifier is the only code path that treats
a clean legacy population as ready to write.

The offline custom-IKM notes in
`dim-product-scd2-ikm-compatibility-preparation.md` and
`odi68-ikm-audit-candidate-preparation.md` describe that older Y/N proposal.
They are historical. This runbook does not adopt a custom IKM.

## Migration executor

`execute_dim_product_stock_migration` in `dim_product_scd2_stock_migration.py`.

- Target is fixed: `DWH.DIM_PRODUCT`. Backup is fixed:
  `DWH.ODI87_DIM_PRODUCT_MIG_BKP`. Callers cannot pass an owner or table.
- The reviewed population pin is 26 columns and 72 rows. `DWH.ODS_SH_PRODUCTS`
  must still have 72 rows, and `DWH.SEQ_DIM_PRODUCT` must still exist. The
  executor never selects `NEXTVAL` and never updates ODS.
- Approval scope: `odi87_dim_product_stock_migration`. Independent review must
  be `approved` and `approved` must be exactly `True` before the connection
  factory is called.
- Absent backup: `CREATE TABLE ... AS SELECT` only. An existing backup is
  reused only when metadata matches and both `MINUS` directions are zero, so
  the backup is the before-image rather than merely the same row count.
- The backup is not dropped on success or failure.
- DML is one serializable transaction. Post-checks compare preserved columns
  (`PRODUCT_SK`, `PROD_ID`, `EFFECTIVE_FROM`, tracked attributes, and the four
  audit columns), map `Y/N` to `1/0`, map only the 9999 sentinel to 2400, and
  require the backup counts to stay unchanged. Failure rolls back. Commit
  happens only after those checks.
- A later call that already sees stock rows is a no-op. It does not create a
  backup of the stock table. If the legacy backup is present, it must still
  be the pre-image.

## Restore executor

`execute_dim_product_stock_migration_restore`.

- Approval scope: `odi87_dim_product_stock_migration_restore`.
- It copies only `CURRENT_FLAG` and `EFFECTIVE_TO` from the retained backup
  back onto matching `PRODUCT_SK` values.
- It does not drop the backup, touch ODS, or advance the sequence.
- A target that already matches the backup is a no-op.
- A stock target whose backup is not the legacy pre-image fails before DML.

## What this slice does not prove

- Native ODI mapping and scenario runtime. Offline Groovy text checks are not
  a repository execution.
- Repository repair or scenario regeneration. The mapping script already
  carries the stock expressions; this slice does not write the ODI repository.
- The direct wrapper `run_dim_product_scd2_scenario_safe` still runs after its
  existing approval and identity checks. It does not re-read flag values.
  The supported scenario-run readiness gate does refuse a candidate report
  whose stock DQ invalid-flag count is non-zero, which is what legacy `Y`/`N`
  rows become under the updated candidate SQL.
- Numeric equality between ODI `SESS_NO` and the SQL batch id.
- Execution of the stock IKM `I$` audit tasks. The SQL path writes the four
  audit columns on the target; it does not invoke the IKM.
- Live migration, a fresh production backup, grants, model changes, table
  cleanup, partition work, or application endpoint changes.
- Generic `mapping_generator` still has a `9999-12-31` default, and generic
  `scd2_postflight` still accepts both sentinels. Those modules are outside
  the fixed DIM path and are unchanged. Fixed DQ does not accept both.
