# Audit Schema Migration Runbook

## Boundary

`ddl/audit_schema_contract.sql` is the only authoritative DDL for the shared
`AUDIT_USER` objects. This runbook does not authorize execution. Live Oracle
metadata inspection and every migration statement require separate explicit
operator approval.

## Phase 1: Static review

1. Run the offline contract tests.
2. Review the canonical table, index, and grant definitions.
3. Do not execute any retired `ddl/faz_*` or `ddl/init_*` entrypoint.

## Phase 2: Metadata-only preflight

Use `audit_schema_contract.build_audit_schema_preflight_plan()` as the reviewed
query contract. Execute only the listed `SELECT` statements after approval.
Evidence must contain table/column/index/grant metadata and counts only. Do not
include raw rows, primary-key values, credentials, or connection strings.

If an existing object differs from the canonical contract, report
`blocked_by_schema_drift`. Do not infer that ORA-955 means the object is valid.

## Phase 3: Migration

Prepare a reviewed migration from the metadata diff. Include forward steps,
rollback steps, and a post-migration metadata check. Obtain a new explicit
approval before running it. Never bundle the migration into application startup
or a normal mapping request.

## Phase 4: Verification

Re-run the metadata-only preflight. A successful result may report contract
status and object counts only. It must not claim success when a required DDL or
grant failed.
