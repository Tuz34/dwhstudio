# Generic Type 2 (SCD2) builder for EDW and Datamart targets

Exposes a parameterised Type 2 path through the app (ODI-81). It generalises the
fixed `DIM_PRODUCT` slice: any source table in `DWH`, `DM`, `HR` or `SH` can
feed a Type 2 target in `DWH` or `DM`.

## Request

```json
{
  "source_owner": "DWH", "source_table": "ODS_CUSTOMERS",
  "natural_key": ["CUST_ID"],
  "tracked_columns": ["CUST_NAME", "CUST_CITY"],
  "overwrite_columns": [],
  "target_owner": "DM", "target_table": "DIM_CUSTOMER",
  "mapping_name": "MAP_ODS_CUST_TO_DIM_CUST",
  "sequence_name": "SEQ_DIM_CUSTOMER"
}
```

`tracked_columns` may be the string `"all"`. `overwrite_columns` are optional
Type 1 attributes (updated in place, no new row). Identifiers must match
`^[A-Z0-9_$#]{1,30}$` (mapping names up to 31 characters so that `<mapping>_SCN`
fits the 35-character scenario limit); `$` is refused because the ODI parameter
transport rejects it. Natural key, tracked and overwrite columns must be in the
source.

## Target contract

`<STEM>_SK` surrogate key (NUMBER, sequence backed, primary key; `DIM_` or `D_`
prefix stripped), natural key columns (NOT NULL), tracked and overwrite columns
(source type and nullability), `EFFECTIVE_FROM` DATE, `EFFECTIVE_TO` DATE,
`CURRENT_FLAG` VARCHAR2(1), `INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM`,
`ETL_BATCH_ID`. For the products metadata this reproduces the frozen 26-column
`DIM_PRODUCT` contract exactly (covered by a test).

## Routes

| Route | Gate | Behaviour |
|---|---|---|
| `POST /api/scd2/plan` | CSRF, 20/min | Preview. Uses `source_columns` from the body, or reads catalog metadata (never rows) when omitted. |
| `POST /api/scd2/generate` | CSRF, `approved: true`, 10/min | ODI probe -> target sequence + table DDL (DDL user) -> datastore -> SCD mapping -> scenario `<mapping>_SCN` v001. Per-step status. |
| `POST /api/scd2/run` | CSRF, `approved: true`, 5/min | Guards, runs the scenario via the safe wrapper, returns count-only preflight and postflight. |
| `GET /api/scd2/postflight` | 30/min | `owner`, `table`, `natural_key` (and optional `source_owner`, `source_table`): counts only. |

Generation is resumable: a target that already matches the contract, an
existing datastore or an existing mapping is reported as `skipped_exists` /
`already_exists_matching`. Nothing is ever dropped or altered; a mismatching
existing target returns `blocked_target_exists_mismatch`. Partial states are
reported (`partial_failure_*`) with what was created and
`cleanup_requires_separate_approval`.

## Datamart (DM)

The target model code is `TGT_DM`. If it does not exist in ODI, generation
returns `blocked_dm_model_missing` before any write, with the topology steps
to perform. The app does not create topology objects.

## Safety

No raw rows, key values, secrets or repository ids appear in any response.
Groovy output is parsed through strict envelopes; unexpected shapes become a
fixed `InvalidGroovyEnvelope` error. The new Groovy actions are registered in
`GROOVY_ACTION_REGISTRY` with the strict structured-error contract.

## Execution user (grantee)

When grants are enabled, the execution account is the Oracle username on the
single data server reached from the target model, the `GLOBAL` context, and the
physical schema whose name is the target owner. The read-only probe uses public
SDK getters (`getCode`, `getLogicalSchema`, `getPhysicalSchema`, `getSchemaName`,
`getDataServer`, `getTechnology`, and the inherited `AbstractOdiDataServer.getUsername`).
It does not read a password, JDBC URL, connection property, or the data server's
topology name, and it does not take the first match.
`ODI_EXEC_USER` is not that proof. A missing, ambiguous, or non-Oracle topology
stops generation with `blocked_execution_identity` before any DDL. The response
carries a reason code and counts, not the account name.
This proof covers `GLOBAL` only; the current run route still accepts other
contexts and must be bound to the proven context in the separate K3 integration
before the coding gate or live acceptance can be declared complete.

An explicit `exec_user` argument must equal that proven account or generation
stops with `execution_identity_rejected`. A request field cannot choose the
account. Preview stays connectionless and does not emit execution grants until
the proof exists. A `SELECT` grant to `APP_USER` remains opt-in with
`"grant_select_to_app_user": true`. Before DDL the executor still checks
`ALL_USERS`; a missing grantee stops with `blocked_grantee_missing` and a
message that does not name the account.

Two cases skip the grants instead of failing:

- `ODI_EXEC_GRANTS_ENABLED=false` (env, default `true`): use this when ODI
  executes as the target owners themselves (for example DWH for DWH targets and
  DM for DM targets) and there is no separate execution user. Generation does
  not query topology for an execution account. The executor then skips the
  `ALL_USERS` grantee check and all grant statements (also in the repair path
  for an existing table), and the plan preview and result report
  `grants_skipped_by_config: true`.
- When the grantee equals the target owner (case-insensitive) a grant would raise
  ORA-01749, so it is skipped and `grants_skipped_owner_is_grantee: true` is
  reported.

Both flags are booleans only; no identities beyond the already-reported
owner and table are added.

## Column metadata registration

ODI renders DDL from column metadata masks (`TIMESTAMP(%L)`, `NUMBER(%L,%P)`), so
the registered length must be valid for the mask. A TIMESTAMP column is
registered with its fractional-seconds precision as the length (default 6, range
0..9) and no scale, never OdiColumn's default of 50 (which makes the IKM's flow
table fail with ORA-30088). Plan construction validates source `TIMESTAMP(n)`:
n in 0..9 is accepted (6 is the bare-TIMESTAMP default; other values flow
through the DDL and registration), larger values are rejected with
`invalid_timestamp_precision`. DATE carries no length, character types keep
their size, and bare NUMBER keeps the unconstrained sentinel (its length reads
back as null or 0, both treated as the sentinel). After registration the
script reads the columns back and fails with `ColumnMetadataMismatch` (class
only) if any length differs from the plan, a TIMESTAMP length is outside 0..9 or
a NUMBER length exceeds 38. The fixed DIM_PRODUCT registration checks that its
TIMESTAMP columns have length exactly 6. The probe compares TIMESTAMP length as that
precision. Datastores registered before this change are not migrated.

The audit columns `INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM` and
`ETL_BATCH_ID` are registered with the SCD behaviour `OVERWRITE_ON_CHANGE`: the
standard IKM loads only columns that carry an SCD behaviour into the I$ flow
table and inserts new dimension rows from I$, so without one these columns
arrive NULL. Their fixed expressions flow through I$ and are overwritten on
matched rows without ever creating a version. This applies to all four,
including `INSERT_DATE`: on a matched row it is overwritten, so it effectively
means "last load timestamp". The mapping script's role contract check expects
these four in addition to the caller's overwrite columns. The other roles are unchanged
(surrogate key, natural key, tracked attributes, `EFFECTIVE_FROM`/`EFFECTIVE_TO`
as start/end timestamp, `CURRENT_FLAG` as current record flag).

## Flow control

The mapping script sets the target IKM options `FLOW_CONTROL` and
`STATIC_CONTROL` to false and reads them back, failing with `IkmOptionMismatch`
(class only) otherwise. Data-quality checks are intentionally off for SCD2
loads: with a CKM present in the repository, flow control defaults to true and
would divert rows to `E$_<target>`.

## Existing objects

Existing target tables, datastores and mappings are verified against the
requested plan (columns, SCD roles, keys, source/target, integration type,
IKM/LKM) before being reused; any difference returns
`blocked_existing_object_mismatch` and nothing is written. An existing matching
table with a missing sequence or grants is repaired (create sequence, add
grants) without altering the table, or reported as `blocked_target_incomplete`.
Knowledge modules: the probe reports `access_point_count` and one LKM name per
access point (an empty string for an unassigned one). With zero access points
(source and target on the same ODI data server) no LKM is required and none may be
listed. Otherwise the list must have exactly one entry per access point and every
entry must be `LKM SQL to Oracle (Built-In)`; the create script forces it on every
access point and fails with `LkmDbLinkRemains` if a DB-Link LKM remains. A KM mismatch response carries
`km_mismatch` with the expected and found KM names (names only, no ids).
`run` is bound to the server-derived `<mapping>_SCN` and a version verified to
exist for that mapping (latest by default).
