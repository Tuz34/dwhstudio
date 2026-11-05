# ODI-81 F1-F3 classic application path

Offline application-path code. Importing the driver does not open a connection
or call an endpoint. Tests use synthetic SQLite and mocked service dependencies.
A mocked generate or run is not a native ODI load. Native live acceptance
remains a later phase.

## Fixed request

| Flow | Target | Mode | Run truncate |
| --- | --- | --- | --- |
| F1 | DWH.ODS_ODI81_F1 | IU, primary key ENTITY_ID | false |
| F2 | DWH.ODS_ODI81_F2 | CONTROL_APPEND full reload | true |
| F3 | DWH.DWH_ODI81_F3 | IU, primary key ENTITY_ID, one row per key | false |

`POST /api/mapping/generate` uses `engine=classic`, literal `approved=true`,
and `context=GLOBAL`. `POST /api/mapping/run` uses scenario version `001` and
the same context. The role-only SCD2 routes are not used. `drop_target_if_exists`
and `replace_existing_mapping` stay false. No surrogate key is requested.

The source model is the internal `ORACLE_DWH` model for `DWH.ODI81_SRC_Fn`.
The builder resolves the approved IKM from the integration type before DDL.
F1 and F3 require a primary key on `ENTITY_ID`. F2 has no update key. An
existing target with a missing column or the wrong key blocks without `ALTER`.

## Order

The F1-F3 source mutation refuses to run until both run-2 backups already
match the seed checkpoint. Target backup R2 is therefore taken after run 1
and before the source change. Run 3 is a second pass over the changed source.

1. Prepare the absent source.
2. Classic generate, then retained empty target backup R1 and before-run verify.
3. Classic run 1 and after-run verify.
4. Retained source backup and target backup R2, then the one update and one insert.
5. Before-run verify, classic run 2, after-run verify.
6. Retained target backup R3, before-run verify, classic run 3, after-run verify.

F1 and F3 run 3 must match the retained full target image, including audit
columns. That witness is still not a physical DML count.
`physical_dml_count_proven` stays false. F2 is a full reload: business rows
match, audit values may change, and the driver does not call that a no-op.

A non-200 response, a skipped or blocked generation, a version or context
mismatch, a bad truncate flag, or a failed verification stops every later
step. Timeouts and malformed responses stay unknown and require manual review.
There is no retry and no drop.
