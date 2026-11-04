# PRODUCTS ODS Type 1 Executor

This review-ready slice prepares the first current-state synchronization from `SH.PRODUCTS` to
`DWH.ODS_SH_PRODUCTS`. No live backup or Type 1 load was run while implementing it.

## Read-only candidate preflight

The preflight uses the target connection and starts a read-only Oracle transaction before counting source rows, target
rows, duplicate natural-key groups, inserts, refreshes, and unchanged rows. It returns count/status/metadata only and
never returns a product key or row value. This also proves that the target connection can read the fixed source object.

## Named backup/restore proof

The first persistent action has exact scope `odi68_products_ods_named_backup_restore_proof`. It creates one fixed named
backup of the current target, creates a second fixed disposable restore copy from that backup, verifies 24 columns and
counts, removes the restore copy, and leaves the named backup available for rollback. Existing proof objects block the
run before DDL. Failure cleanup is limited to fixed objects created by that same call.

## Type 1 load and rerun proof

The load has a different exact operator approval scope: `odi68_products_ods_type1_load_and_rerun_proof`. The executor
requires approved review, a ready candidate preflight, and a passed named backup/restore proof before opening a
connection. It starts a serializable transaction, rechecks candidate counts and the named backup, then runs one fixed
MERGE statement twice in the same transaction. The second execution must affect zero rows. Count reconciliation,
duplicate-key checks, and zero remaining insert/refresh candidates must all pass before commit; otherwise the entire
transaction rolls back.

## Safety boundary

- fixed `SH.PRODUCTS`, `DWH.ODS_SH_PRODUCTS`, backup, and restore-proof objects only.
- exact operator approval is separate for backup proof and Type 1 load.
- backup remains after a successful load until a later reviewed cleanup or rollback decision.
- no endpoint and no ODI mapping/scenario or repository write.
- no raw rows, no PK values, and no secret or connection material in evidence.
- Oracle errors return fixed status/stage plus `error_type`, never driver text.
