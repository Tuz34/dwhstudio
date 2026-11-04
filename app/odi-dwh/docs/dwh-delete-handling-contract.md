# Phase 4 - Soft Delete Static Contract

This metadata-only contract covers delete handling for `SH.SALES -> DWH.ODS_SH_SALES`. It does not authorize live work.

## Frozen contract

- Policy: soft delete; hard delete is not allowed.
- Detection: in-database count-only anti-join using the reviewed fact grain.
- Metadata columns: `DELETED_FLAG` and `ODS_DELETED_AT`.
- Evidence: candidate, newly marked, already marked, active, and total counts only. Candidate keys are never returned.
- Rerun: records already marked are a no-op.

## Backup and rollback

Pre-apply flag counts and the reviewed batch marker must support restoration before any persistent action. Schema change,
candidate marking, DQ checks, and rollback each remain separate reviewed steps.

## Safety boundary

- no raw rows; no PK values; no secrets or connection material.
- no executable statements, connection, hard delete, scenario run, or load.
- no persistent action without exact operator approval for the count-only preflight result and named batch.
