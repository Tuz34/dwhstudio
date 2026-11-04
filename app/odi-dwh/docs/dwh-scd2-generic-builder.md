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

DML grants go to the Oracle user named by the config key `ODI_EXEC_USER`
(default `ODI_EXEC`). Before any DDL the executor checks `ALL_USERS`; if the
user does not exist, generation stops with `blocked_grantee_missing` and a
message naming the missing user, with no DDL executed. A `SELECT` grant to
`APP_USER` (needed so the app's own count-only postflight can read the target)
is opt-in with `"grant_select_to_app_user": true` in the request.

## Existing objects

Existing target tables, datastores and mappings are verified against the
requested plan (columns, SCD roles, keys, source/target, integration type,
IKM/LKM) before being reused; any difference returns
`blocked_existing_object_mismatch` and nothing is written. An existing matching
table with a missing sequence or grants is repaired (create sequence, add
grants) without altering the table, or reported as `blocked_target_incomplete`.
`run` is bound to the server-derived `<mapping>_SCN` and a version verified to
exist for that mapping (latest by default).
