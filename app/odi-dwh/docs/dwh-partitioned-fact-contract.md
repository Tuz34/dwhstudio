# Phase 3 - Partitioned Fact Static Contract

This metadata-only contract covers `SH.SALES -> DWH.ODS_SH_SALES`. It does not authorize live work.

## Frozen contract

- Partition key: `TIME_ID`; reviewed grain: monthly for the current count-scale evidence.
- Migration route: create a parallel target, backfill, reconcile, then perform an operator-controlled swap.
- Window source: the existing `TIME_ID` watermark and rerun guard.
- Rerun scope: only the reviewed window partition; a full-target reload is not allowed by this contract.
- Evidence: target/window/unaffected-partition counts and watermark status only.

## Backup and rollback

The current target and watermark state must remain restorable. No migration starts until the parallel target, backup
artifact, restore procedure, read-only partition capability preflight, and rerun result have separate review evidence.

## Safety boundary

- no raw rows; no PK values; no secrets or connection material.
- no executable statements, connection, partition migration, scenario run, or load.
- no persistent action without exact operator approval for the named target and migration step.
