# ODI-81 F4/F5 SCD2 acceptance harness

Offline fixture and application-path code. Importing either module does no IO.
No live database, ODI, or HTTP call is made by the tests. The frozen F1-F3
helper is imported and not edited. A full eleven-column witness for F1-F3
remains on the frozen snapshot-noop change and is not claimed here.

## Fixed manifest

| Flow | Source | Target | Surrogate | Mapping |
| --- | --- | --- | --- | --- |
| F4 | DWH.ODS_ODI81_F4 | DWH.DIM_ODI81_F4 | ODI81_F4_SK | MAP_ODI81_F4 |
| F5 | DWH.DWH_ODI81_F5 | DM.DIM_ODI81_F5 | ODI81_F5_SK | MAP_ODI81_F5 |

The surrogate names are `surrogate_key_name` of the target table. They are the
same `*_SK` family the generic planner emits. They are not `PRODUCT_SK`.

Each source is `ENTITY_ID NUMBER(10)` primary key, `ATTR_TEXT VARCHAR2(40 BYTE)`,
`VALUE_AMT NUMBER(10,2)`. Three internal synthetic rows are seeded. One update
and one insert produce four rows. The values match the F1-F3 helper and are not
copied into reports.

Each target has those three business columns, the surrogate, and the seven
stock/audit columns from the generic planner. `UPDATE_DATE` stays nullable in
the dictionary because that is the planner contract. A NULL value in any of the
eleven columns, including `UPDATE_DATE`, is not a witness.

Stock flags are `1` current and `0` historical. The open end is
`DATE '2400-01-01'`. A current row must end on that date. A historical row must
end before it, and that end must equal the current version's `EFFECTIVE_FROM`
for the same `ENTITY_ID`. The generic literal `ODI_SCD2` is not required; the
fixed DIM_PRODUCT source-system literal is not applied to these fixtures.

Expected shapes:

Every run now has a distinct, freshly executed `before_run` verification and
approval scope, including runs 2 and 3. Before run 2 the target must still match
the retained seed checkpoint, not the source that has already been changed.
Retained backup equality requires actual row totals and unique/non-null row
identities in addition to both MINUS projections; duplicate identical rows
cannot count as a valid backup. Source and target metadata remain fixed.

After run 2 and on run 3, the retained run-2 before-image also proves the expired
version's original business values, surrogate, start date and audit values. Only
its current flag/end date may change. The two unchanged current rows must retain
their complete projections, and changed/new rows must receive fresh surrogates.
This prevents a history-shaped overwrite from passing as Type 2.

HTTP writes are marked attempted and uncertain before invoking the client.
Timeouts, unknown or malformed responses require manual reconciliation and stop
all downstream work without retry. Only a verified successful response clears
that call's uncertainty. The driver and individual fixture gates accept only
GLOBAL before any callback, even if another context has a syntactically valid
binding. These are offline-tested code invariants, not native load evidence.

| Checkpoint | Current | Historical | Total |
| --- | --- | --- | --- |
| Before run 1 | 0 | 0 | 0 |
| After run 1 | 3 | 0 | 3 |
| After run 2 | 4 | 1 | 5 |
| After run 3 | 4 | 1 | 5, and the full 11-column image matches the run-3 backup |

`before_run` never sets `full_target_snapshot_equal`. Only a passed `after_run`
of run 3 does, and only after both directions of the eleven-column comparison
are empty. That witness is not a physical DML count.
`physical_dml_count_proven` stays false. `rerun_semantics` is `incremental`
only on that passed run-3 witness. F4 and F5 are not full reloads.

## Gates

Every public fixture call requires literal `approved=True` and one exact scope
before a factory is called. The acceptance driver also requires literal
`live=True`, every scope for that flow, an injected HTTP client, and injected
writer/reader factories before the first callback. The default `live` value is
false. `python odi81_app_acceptance.py` prints a refusal and does not build a
client.

The writer factory is per owner. `SESSION_USER` must be that owner. `SELECT ANY
TABLE` does not make a writer acceptable. F5 target backups and the F5 target
lock use the DM factory. Source work uses the DWH factory.

The configured source reader is proved with
`source_model_policy.trusted_source_model_code`. The binding is an injected
JSON string, not a file or environment value. The proved code must equal
`SOURCE_MODEL_BY_OWNER` for that owner. A missing pair, a wrong context, invalid
JSON, or a code that disagrees with the app stops before any factory or HTTP
call. Generate and run bodies do not carry an execution user or a model override.

Absent objects are created once. A preexisting object with the same dictionary
and the same checkpoint is kept. A collision or a metadata/checkpoint mismatch
stops the step. Retained backups are never dropped, truncated, or altered.
Oracle DDL commits implicitly: `ddl_attempted` and `object_may_exist` are set
before `CREATE`. A DML commit that does not return is `outcome_unknown` and is
not reported as no effect. Locks and fresh dictionary/count checks happen
before the change.

Read-only work sends `SET TRANSACTION READ ONLY` before any other statement on
that connection. Cleanup uses a callback stack so a later close still runs when
an earlier close raises `KeyboardInterrupt`, `SystemExit`, or `GeneratorExit`.
Ordinary callback failures are redacted by the existing F1 operation boundary.
The new modules do not add an `except Exception` handler.

Reports contain status, counts, and booleans. They do not contain row values,
key values, SQL text, binds, or exception text.

## Application order

F4 and F5 call the real routes through the injected client:

1. prepare source
2. `POST /api/scd2/generate`
3. retained target backup R1
4. before-run verification (not an after-run witness)
5. `POST /api/scd2/run` for run 1, then verify
6. retained source backup, then the one update and one insert
7. retained target backup R2, run 2, verify
8. retained target backup R3, run 3, full eleven-column verify

Scenario version is `001`. The run context is the context that already matched
the binding. A non-200 response, an unexpected status, a version or context
mismatch, a failed verification, or a missing backup stops every later step.
There is no retry and no cleanup/reset.

F1-F3 prepare through the existing helper, then stop with
`app_target_contract_gap`. Their targets are ODS or type-1 tables. Calling
`/api/scd2/generate` would create a surrogate SCD2 table and commit that DDL.
`/api/mapping/generate` is not this SCD2 path. The driver does not invent a
success witness for that gap.
