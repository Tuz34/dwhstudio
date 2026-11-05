# Generic SCD2 stable audit expressions (ODI-87 K3)

## Problem

The generic builder marks the four audit columns `INSERT_DATE`, `UPDATE_DATE`,
`SOURCE_SYSTEM` and `ETL_BATCH_ID` as overwrite-on-change. It used to set volatile
creation expressions (`CURRENT_TIMESTAMP` and the ODI session number). Because the
stock IKM compares overwrite columns, an unchanged source row then refreshed its
audit values on every run. That broke the F4/F5 full-snapshot no-op witness.

## Rule

Each audit expression is a guarded `CASE`. The stock 1/0 + 2400 profile, the stock
IKM and its options, the surrogate-key `TARGET` hint and every other expression are
unchanged.

| Audit column | Kept from the matching current version when | Otherwise |
| --- | --- | --- |
| `INSERT_DATE`, `SOURCE_SYSTEM` | every natural-key column is equal and every tracked attribute is equal (null-safe) | creation default |
| `UPDATE_DATE`, `ETL_BATCH_ID` | the same, and every user overwrite attribute is also equal (null-safe) | creation default |

- A user overwrite (type 1) change keeps the version's creation audit values but
  refreshes `UPDATE_DATE` and `ETL_BATCH_ID`. The overwrite feature stays enabled.
- A new natural key or a changed tracked attribute gets the creation defaults.
- Only current rows (`CURRENT_FLAG = '1'`) can match. History rows never do.
- An existing `NULL` audit value is kept as `NULL`. No null-replacement function is used.
- Null-safe equality is Oracle `DECODE(a, b, 0, 1) = 0`, which treats two `NULL`s as equal.
- Composite natural keys are supported. Identifiers are sorted, so the text does not
  depend on plan order versus datastore-role order.
- If more than one current version matches, Oracle's scalar subquery raises
  ORA-01427 and the run fails. Nothing limits rows or picks one.

## Inputs

Identifiers are validated (`^[A-Z0-9_#]{1,30}$`) before any text is built. The
target reference is the plan's `owner.table`. The source alias is the mapping's
source component name. It must be a valid identifier and must differ from the
correlation alias `SCD2_CUR`; otherwise the builder stops with `SourceAliasInvalid`.

## Builder

`scd2_mapping_create.groovy` records every expression it sets. After the final
validation it re-reads all target attributes and stops with `MappingExpressionDrift`
in these cases:

- any expression changed;
- an audit expression is forced to execute on the target;
- fewer than four stable audit expressions remain.

Nothing is repaired after that gate. The report carries only
`stable_audit_expression_count`, never expression text.

## Probe and reuse

`scd2_probe.groovy` holds a byte-identical copy of the text builder; a test compares
both blocks. The probe re-derives the expected texts from these inputs:

- the target datastore roles: natural key, tracked, and overwrite minus the four audit columns;
- the physical target owner, which `scd2_odi.probe` derives from the validated target model;
- the mapping's single source alias.

It reports only booleans and counts in `mapping.audit_profile`: `stable`,
`role_contract_valid`, `source_alias_valid`, `audit_attribute_count`,
`stable_expression_count` and `audit_target_hint_count`.

`scd2_odi` rejects a malformed profile, which fails the probe. `mapping_mismatches`
adds `mapping_audit_profile` unless the profile is complete and stable. The generate
route and the run route therefore refuse to reuse an existing mapping with a missing,
volatile or partial profile. Existing generic mappings built before this change are
blocked, not repaired; a separately reviewed repair or rebuild is required.

## Evidence and limits

These tests run on fakes and SQLite; they are not native proof:

- The real Groovy text builder generates CASE texts. Those texts run on SQLite through
  a small adapter: `DECODE` becomes `IS`, and the ODI session token and
  `CURRENT_TIMESTAMP` are replaced by fixed markers. The F4/F5 shapes and a composite
  key with a user overwrite attribute are covered.
- The complete builder script runs on the isolated fake SDK, including tampering with
  expressions and the execute hint during the final validation.
- The probe's profile section runs as a fragment against synthetic datastore and
  mapping objects.
- The probe, generate and run boundaries run in Python for F4/F5.

Not proven:

- native Oracle execution of the generated SQL;
- stock IKM handling of these expressions in the flow table;
- the ORA-01427 behaviour on ambiguity;
- the live F4/F5 rerun no-op.

This is not K3 completion and not live acceptance.
