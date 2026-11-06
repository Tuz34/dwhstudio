# ODI81 F1-F3 fixture preparation

Preparation code, not live approval or acceptance evidence. No DB/ODI/API call
was made during implementation. Main must independently review this slice.
No SCD2, F4/F5, existing metadata repair, cleanup, restore, grants, automatic
retry, or source-owner fallback is implemented.

## Fixed manifest

| Flow | DWH source | DWH target | Later classic behavior |
| --- | --- | --- | --- |
| F1 | ODI81_SRC_F1 | ODS_ODI81_F1 | IU / primary ENTITY_ID |
| F2 | ODI81_SRC_F2 | ODS_ODI81_F2 | CONTROL_APPEND / truncate each run |
| F3 | ODI81_SRC_F3 | DWH_ODI81_F3 | IU / primary ENTITY_ID / Type 1 |

Each source has exactly ENTITY_ID NUMBER(10) NOT NULL with one enabled,
validated primary key; ATTR_TEXT VARCHAR2(40 BYTE) NOT NULL; VALUE_AMT
NUMBER(10,2) NOT NULL. Three synthetic rows are internal constants. No row,
key, attribute value, SQL text, exception message/class, or credential is
included in returned evidence.

## Interfaces and gates

New module: `odi81_isolated_fixtures.py`. No existing caller is wired to it.
All three entry points require literal `approved=True`, one exact flow F1/F2/F3,
and that flow's exact scope BEFORE any connection factory is called:

- `prepare_source(flow, writer_factory, source_reader_factory, ...)`:
  scope `odi81_f1_prepare_source` (substitute f2/f3). One absent-only CREATE,
  three internal inserts, one explicit seed commit, actual-reader postflight.
- `mutate_source(flow, writer_factory, source_reader_factory, ...)`:
  scope `odi81_f1_mutate_source`. Exactly one attribute update plus one insert
  in one transaction, one commit, actual-reader postflight. The original seed
  must still match exactly; repeating the mutation blocks.
- `verify_flow(flow, run, phase, reader_factory, source_reader_factory, ...)`:
  scope `odi81_f1_verify_flow`; run is integer 1/2/3 and phase is `before_run`
  or `after_run`. Always read-only; no scenario is invoked or authorized.

Factories must return fresh, dedicated Oracle connections with autocommit off,
not an application connection carrying unrelated work. The writer must report
SESSION_USER DWH. Pass the **actual configured source-reader factory**, not a
privileged substitute. Source capability/read failures stop; no GRANT is issued.
For an absent source, the reader must already be DWH or have SELECT ANY TABLE;
there is no object yet on which to prove a per-object grant. This deliberately
conservative pre-DDL gate is not a recommendation to grant broad privileges.
The real fixture SELECT and metadata/count checks run after seed commit; if
they fail, report failed/partial and retain the created table for manual review.
For existing fixtures, actual qualified SELECTs are mandatory before mutation
or verification, regardless of the writer's privileges.

Connection/interface binding at main `dc7f9685dbcd4d6aaf4b2a6d960fa69a577e26df`:
`source_connector.OracleSourceConnector._connect` is the configured Oracle
source connection; `db.get_connection(use_ddl=True)` is the DDL connection.
Classic ORACLE_ODS/ORACLE_DWH source metadata currently goes through
`target_table_service.read_table_schema` and the DDL connection; that does
**not** prove source-reader access. A future reviewed wrapper must explicitly
bind the actual source connection and verify ODI physical source topology.
Do not import config or construct these live connectors in offline tests.

Before any future persistent preparation, the coordinator must independently
establish approved effective configuration, remote agent health, topology,
exact IKM/key ownership and operator approval. Application runs are a separate
integration: the common remote PR must be merged and deployed before actual
testing. At this base, `/api/mapping/run` is still the pre-remote version; this
module intentionally does not call it. Rebind to the final deployed interface.

## Backup and verification boundaries

`create_retained_backup` creates one absent checkpoint by explicit column DDL
and `INSERT SELECT`, then commits. An existing permanent table is retained
only when its metadata and checkpoint already match. A view, a column drift,
or a row drift blocks before any copy, and the object is not altered or
dropped. Missing backups still block mutation and verification. A comparison
that runs after the catalog reader opens and then disagrees is failed. A
factory error before that open stays blocked.

Before source mutation, require `DWH.ODI81_Fn_S_R2_BKP` and
`DWH.ODI81_Fn_T_R2_BKP`, both matching the three-row seed checkpoint. The source
is locked EXCLUSIVE and target/backups SHARE NOWAIT through commit. Metadata,
unique/non-null business keys, counts, and bidirectional full-projection
comparisons must pass before either DML statement.
All four names must pass permanent-table validation before the first lock;
full metadata and checkpoint checks are repeated after locking. Enabled source
table triggers are rejected using a count-only ALL_TRIGGERS check under the
source lock, before seed or mutation DML. No trigger is disabled or dropped.
This is not a general database/schema-trigger audit. Concurrent object DDL must
remain excluded by the operator; a kind precheck is not an atomic namespace lock.

Before **every** target run, including all F2 truncations, use `verify_flow`
with `before_run` and require `DWH.ODI81_Fn_T_R1_BKP` (empty), T_R2_BKP (3 seed
rows), or T_R3_BKP (4 changed rows). It compares full target/backup projections
at this checkpoint. No result authorizes a truncate itself. Backups must remain
retained and unchanged; unknown execution stops, not retries. Another process
can change objects after a read-only check; future orchestration must serialize
the flow and rerun the gates immediately before the separately approved run.

After each app run, `after_run` requires source/target business equality in
both directions and unique/non-null keys: run 1 has 3 rows; runs 2/3 have 4.
Run 2 vs T_R2_BKP requires one changed existing, two unchanged, one inserted;
run 3 vs T_R3_BKP requires zero changed/inserted and four unchanged, plus an
bidirectional comparison of every fixed target column, including audit
and timestamp fields. `full_target_snapshot_equal=true` is possible only after
successful `after_run` verification of run 3 with identical full projections;
before-run checks cannot prove a rerun. F1/F3 require this equality. F2 is a
full reload: it records unequal audit values without misclassifying that valid
reload as a failed incremental no-op, and labels `rerun_semantics=full_reload`.
`rerun_semantics` is set only for that completed after-run of run 3. Earlier
passes and `before_run` leave it unset. Any read or cleanup failure leaves the
snapshot witness false and resets `logical_business_comparison_only` to false
and `rerun_semantics` to unset. Retained
backup business rows are checked against internal checkpoint constants, not
against a source intentionally changed since backup creation.

Run 1/2 business comparisons exclude the current app-generated fields:
F1/F2: INSERT_DATE, UPDATE_DATE, SOURCE_SYSTEM, ETL_BATCH_ID.
F3 additionally: CURRENT_FLAG, EFFECTIVE_FROM, EFFECTIVE_TO. These names do
not imply Type 2: F3 must still have exactly one row per business key. Target
column names/counts and the three business type contracts are checked;
backup-vs-original metadata checks include every column's ordinal, type,
length, precision, scale, character semantics and nullability. This is not
an independent verifier of ODI keys, IKM assignment, or audit default formulas.
Run 3 includes all these columns in its retained-target snapshot equality check.
The regression covers every non-business column of that flow, including F3
CURRENT_FLAG, EFFECTIVE_FROM, and EFFECTIVE_TO. Equal values prove logical and
audit idempotence for that checkpoint only when those values are populated.
All-NULL equality is vacuous: MINUS treats two NULL projections as the same set,
and that result is not proof that audit, flag, or effective-date columns were
filled or left unchanged. The offline fixtures seed those columns as NULL, so a
green equality there is not populated-audit evidence. Equality also does not prove
zero physical DML: `physical_dml_count_proven` deliberately stays false. In particular,
the truncate/reload F2 flow must not be described as a physical no-op. F1/F3
fail on refreshed audit fields; F2 can pass business reload checks while reporting
full snapshot equality false. It never claims unchanged audits from row counts.

All comparisons use aggregates; SQL MINUS is paired with total counts and
key uniqueness so duplicate multiplicity cannot pass as set equality. Each
reader's first SQL is SET TRANSACTION READ ONLY, then rollback/close. Evidence
proves only logical business equality, never physical zero DML. F2 is a reload.

## Failure evidence and offline tests

Oracle CREATE implicitly commits. `ddl_attempted` and `object_may_exist` are
set before executing it, even if execution raises. No DROP/reset follows an
error. Seed/mutation commit attempts and confirmed completion are separate;
a commit exception sets `outcome_unknown`, even after a successful rollback.
Cleanup flags can override success. `automatic_ddl_rollback_performed` is always
false. Counts describe observations, not proof that an uncertain commit failed.
All ordinary callback exceptions, including unexpected KeyError/custom Exception
subclasses, become fixed error codes without exception text/class. Cleanup attempts
rollback, cursor close and connection close independently for every resource,
even if an earlier attempt raises an unexpected exception. Nested `finally`
blocks still run the later releases, and the remaining resources, when rollback
or a close raises `KeyboardInterrupt`, `SystemExit`, or `GeneratorExit`. That
process-control exception then propagates. Ordinary exceptions are redacted into
cleanup flags and are not suppressed as process control.

The quality baseline explicitly raises only `broad_except_max` from 300 to 302
for these two audited handlers in `odi81_isolated_fixtures.py`: `_operation`
redacts arbitrary ordinary callback failures into fixed report codes, and
`_release` continues every remaining step of `_cleanup` after an unexpected
ordinary error. Remembering a process-control exception until the other
releases finish, then re-raising it, is not an `Exception` handler and does not
add to that budget.
Narrow exception lists let callback errors escape; suppressing cleanup errors
would lose the failure evidence. This reviewed budget delta retains both safety
boundaries without hiding catches behind aliases or weakening any other gate.
The paused backup-creator draft is excluded from this batch and contributes zero
broad handlers. The fixture-only production manifest has 302, versus 300 in the
tracked base. Regression coverage includes custom exceptions, cleanup continuation,
commit uncertainty and unsuppressed process-control exceptions.
Any non-passed report stops all dependent actions and requires operator review
when the report says so; no automatic retry is safe.

Run only the new offline suite:
`python -B -m pytest -q -o addopts= -p no:cacheprovider tests/test_odi81_isolated_fixtures.py`.
It uses synthetic Oracle metadata fakes and in-memory SQLite for actual
business projections/joins/set differences, translating only Oracle MINUS and
fixed type syntax. Oracle DDL implicit commit and cleanup/commit failures are
simulated; this does not replace eventual Oracle acceptance. Tests never
import application config or open a real DB connection.
