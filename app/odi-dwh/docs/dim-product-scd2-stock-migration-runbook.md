# DIM_PRODUCT stock migration runbook

This is the current fixed-path contract for `DWH.DIM_PRODUCT`. It records the
owner decision to move that table onto stock IKM literals with a retained
backup. It does not apply the migration, open a database connection, or change
the ODI repository.

The fixed path is **NOT FULLY CUT OVER**. The SQL executor below is a reviewed
`UPDATE` of one table. It is not a stock IKM execution, and it does not
authorize an existing ODI scenario.

## Current contract

Era: `stock_ikm_1_0_2400`.

- Current row: `CURRENT_FLAG = '1'` and `EFFECTIVE_TO = DATE '2400-01-01'`.
- Historical row: `CURRENT_FLAG = '0'` and a closed `EFFECTIVE_TO` before
  `DATE '2400-01-01'`.
- `CURRENT_FLAG` stays `VARCHAR2(1)`. `'1'` and `'0'` fit that type, so the
  data cutover is one `UPDATE`. A type change is not part of this slice.
- `EFFECTIVE_TO` stays `DATE`. The migration `UPDATE` rewrites only an exact
  midnight `DATE '9999-12-31'` to `DATE '2400-01-01'`. A time part on that
  calendar day stays in place and is rejected by the classifier before any
  persistent statement.
- Dedicated mapping expressions already use `TO_DATE('24000101','YYYYMMDD')`
  and `CURRENT_FLAG` expression `1`. Load SQL, candidate preflight, read-only
  preflight, and the attribute-change proof use the same stock literals.
- New SQL versions write `INSERT_DATE` and `UPDATE_DATE` with `SYSTIMESTAMP`,
  `SOURCE_SYSTEM` `'SH'`, and `ETL_BATCH_ID` from the approved batch id. The
  migration `UPDATE` does not change those four columns.

Stock DQ accepts only `'1'` and `'0'`. A historical `EFFECTIVE_TO` on or after
`DATE '2400-01-01'` is invalid, including midnight `2400-01-01`, a later
closed date, and `9999-12-31` with or without a time part. A current row whose
`EFFECTIVE_FROM` is after `DATE '2400-01-01'` is invalid. A current row that
does not end at `2400-01-01` is invalid. Mixed rows fail closed. A closed
historical end before `2400-01-01` stays valid, and only its flag changes
during migration.

Mapping expressions for the four audit columns exist in
`odi_automation/dim_product_scd2_mapping_create.groovy`. The datastore register
sets `scd: null` on `INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM`, and
`ETL_BATCH_ID` (`dim_product_scd2_datastore_register.groovy` lines 52-55). The
expected SCD role map in the mapping script does not assign those columns.
Expressions in the Groovy source do not prove that a stock IKM `I$` task
populates them. An earlier live run of this mapping, without those roles,
left the columns out of `I$` and the insert failed with `ORA-01400`. This
patch does not re-run that path.

## Pre-migration contract

Era: `pre_migration_yn_9999`.

- Current row: `CURRENT_FLAG = 'Y'` and `EFFECTIVE_TO = DATE '9999-12-31'`
  at midnight.
- Historical row: `CURRENT_FLAG = 'N'` and a closed `EFFECTIVE_TO` before
  `DATE '2400-01-01'`.

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
- `connection_attempted` is recorded before the factory call, including when
  the factory raises.
- The executor refuses a connection whose `autocommit` is anything other than
  `False`, before any statement. A missing or raising `autocommit` attribute
  fails closed. The executor does not assign `autocommit`.
- Enabled triggers on `DIM_PRODUCT` or on the migration backup block before
  `CREATE TABLE` and again after the locks, before `UPDATE`. Trigger and
  constraint checks are counts and status metadata. The executor does not read
  `SEARCH_CONDITION`.
- `DIM_PRODUCT` must have one enabled, validated primary key, on
  `PRODUCT_SK` alone. Disabled constraints, constraints that are not
  validated, and constraint types other than `P` and `C` block. Oracle stores
  `NOT NULL` as a check constraint, so the accepted shape is
  `check_constraint_count = not_null_column_count = 24` (the fixed non-null
  column count). An extra check makes those counts diverge and blocks. The
  backup table does not need a primary key.
- Absent backup: `CREATE TABLE ... AS SELECT` only. That statement commits, so
  the exclusive locks that protect the final `UPDATE` are taken after it.
  An existing backup is reused only when metadata matches and both `MINUS`
  directions are zero, so the backup is the before-image rather than merely
  the same row count.
- The work transaction is `SET TRANSACTION ISOLATION LEVEL READ COMMITTED`,
  then `LOCK TABLE "DWH"."DIM_PRODUCT" IN EXCLUSIVE MODE NOWAIT`, then the
  same lock on the backup when the backup exists. Evidence is read again
  after those locks and held through commit. Lock contention fails closed.
  The executor does not wait, and it does not use `SERIALIZABLE`.
- Backup reporting is three facts plus retention. `backup_create_attempted`
  records that this call reached `CREATE TABLE`. `backup_may_exist` is true,
  false, or unknown from a live read in this call. `backup_verified` is true
  only when that live read shows the exact before-image or the legacy
  pre-image. `migration_backup_retained` is true only when that live read
  shows the table exists. Caller evidence and the attempted flag do not
  verify or retain a backup. A `CREATE` error is reconciled on a fresh
  connection: the table may be absent, or the statement may have committed
  before the acknowledgement was lost. The executor does not drop or overwrite
  the backup.
- Post-checks compare preserved columns (`PROD_ID`, `EFFECTIVE_FROM`, tracked
  attributes, and the four audit columns), map `Y`/`N` to `1`/`0`, map only
  midnight `9999-12-31` to `2400-01-01`, and require the backup counts to stay
  unchanged.
- `commit` returning is not enough. A fresh connection re-reads under
  `READ COMMITTED` and `LOCK TABLE ... NOWAIT`, then rolls back that
  observation. That second lock is not another migration. When the fresh read
  proves the stock state, the status is `passed`. When `commit` returns and
  the fresh read fails or disagrees, the status is `committed_unverified`:
  `committed` is true, `rolled_back` is false, and a person reconciles it.
- When `commit` itself raises, the status is `outcome_unknown`. `committed`
  and `rolled_back` stay unknown. A rollback acknowledgement would not prove
  that a committed update was undone, so this path does not call rollback and
  does not retry or restore. A fresh read then reports `proven_post_state`,
  `proven_pre_state`, `differs`, or `unavailable`. Unknown stays unknown when
  the fresh read cannot prove a state.
- The backup is not dropped on success or failure.
- A later call that already sees stock rows is a no-op. It does not create a
  backup of the stock table. If the legacy backup is present, it must still
  be the pre-image. A stock backup that is not that pre-image blocks before
  the factory.

## Restore executor

`execute_dim_product_stock_migration_restore`.

- Approval scope: `odi87_dim_product_stock_migration_restore`.
- It uses the same autocommit refusal, trigger and constraint gate, and
  `READ COMMITTED` plus `NOWAIT` locks. Locks are taken before a no-op and
  before the restore `UPDATE`.
- It copies only `CURRENT_FLAG` and `EFFECTIVE_TO` from the retained backup
  back onto matching `PRODUCT_SK` values. Rows that exist only on the target
  stay in place.
- It does not drop the backup, touch ODS, or advance the sequence.
- A target that already matches the backup is a no-op.
- A stock target whose backup is not the legacy pre-image fails before DML.
- A historical end on or after `2400-01-01` fails before DML, so restore does
  not erase that date.
- Commit success and commit uncertainty use the same rules as migration.

## Failure reporting and verification limits

Unexpected adapter exceptions are captured at the same stage-aware boundary as
driver exceptions. A lost commit acknowledgement remains `outcome_unknown`;
a failed fresh proof after an acknowledged commit is `committed_unverified`.
Failed rollback requires manual review, including restore and migration with
an already-existing backup. Reports and cleanup logs never contain exception
messages. Process termination (`KeyboardInterrupt` or `SystemExit`) is not
swallowed and is not a rollback proof.

The trigger guard checks table-level entries in `ALL_TRIGGERS`, not schema-level
or database-level triggers. Those remain a live operational review requirement.
`migration_backup_retained: false` means retention was not proven by this call;
it does not prove that the backup is absent. A separate connection can be unable
to acquire the proof lock while the original uncertain transaction is alive.
Never drop or automatically recreate a backup based on that flag.

The row model is supplemented by the frozen, reviewed SQL contract in
`tests/fixtures/odi87_stock_sql_contract.json`. SQL edits require an explicit
review of that fixture; tests never refresh it. These checks pin lock modes,
NOWAIT, metadata predicates, CTAS, UPDATE and date bounds, but do not execute
Oracle SQL and do not establish live database behavior.
The row model uses static guard counts and does not enforce transaction statement
ordering. Independent reviewer probes check ordering separately; neither is an
Oracle integration test. A post-CTAS failure must not reuse pre-CTAS evidence to
claim the backup is absent; unavailable fresh proof keeps its existence uncertain.

## Remaining cutover work

The fixed path is **NOT FULLY CUT OVER**. These dependencies stay unresolved:

1. Native ODI mapping and scenario runtime. Offline Groovy text checks are
   not a repository execution.
2. Repository mapping or scenario regeneration and repair. The Groovy source
   already carries the stock flag and end-date literals. This patch does not
   rewrite deployed repository objects.
3. `run_dim_product_scd2_scenario_safe` does not re-read flag values. The
   supported scenario-run readiness gate does refuse a candidate whose stock
   invalid-flag count is non-zero. That gate is not this migration.
4. Numeric equivalence of ODI `SESS_NO` and the SQL `:batch_id` value.
5. The stock IKM `I$` audit task is not executed here. Audit SCD roles on
   `INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM`, and `ETL_BATCH_ID` are null.
   Mapping expressions do not prove `I$` population.
6. `mapping.validate()` at `dim_product_scd2_mapping_create.groovy:254` is
   called and its return is not inspected before the repository commit. That
   script does not set a CKM or `FLOW_CONTROL`.
7. The scenario version pin is `001` (`odi_orchestrator.py:113`,
   `dim_product_scd2_scenario_run.groovy:8`). Other existing scenario versions
   are not authorized. No existing scenario is authorized as this cutover.
8. The retained ODI-68 backup `DWH.ODI68_DIM_PRODUCT_BKP` is a separate
   allowlist. Its equality check is expected to stop matching after flag and
   end-date values change. This patch does not widen that allowlist and does
   not use that table as the migration backup.
9. Live migration, a fresh production backup, grants, model changes, table
   cleanup, partition or delete work, and live configuration remain outside
   this patch.
10. Generic `mapping_generator` still defaults `EFFECTIVE_TO` to
    `TO_DATE('9999-12-31','YYYY-MM-DD')`. Generic `scd2_postflight` still
    accepts both `2400-01-01` and `9999-12-31`. Those modules stay unchanged.
    Fixed DQ accepts only the stock literals. The pre-migration literals are
    accepted only by the migration preflight, and only before the cutover.
11. The executor stays pinned to the reviewed 72-row, 26-column population.
    The sequence and ODS are untouched.
12. SQL-path audit writes are `SYSTIMESTAMP`, `'SH'`, and the approved batch
    id. They are not a stock IKM proof. The migration `UPDATE` does not touch
    audit columns.

A passing SQL migration test shows the fixed `UPDATE` and its guards. It does
not show a stock IKM run, a native scenario run, or a completed cutover.
