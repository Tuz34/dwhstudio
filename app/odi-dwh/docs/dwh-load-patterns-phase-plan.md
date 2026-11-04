# DWH Load Patterns Phase Plan

This document is the metadata-only planning artifact for ODI-68. It does not
start an implementation phase and does not authorize live operations.

## Global Safety Boundary

- count/status/metadata only for planning and evidence.
- no raw rows.
- no PK values.
- no secrets or credential material.
- no DDL.
- no DML.
- no ODI scenario run.
- no WebLogic, Docker, Task Scheduler, or service change.
- no Git or Gitea workflow.
- every future live proof requires explicit operator approval.

## Phase 1 - Product Type 1 ODS, then Type 2 dimension

Layer order:

1. `SH.PRODUCTS -> DWH.ODS_SH_PRODUCTS`: Type 1 current-state synchronization.
2. `DWH.ODS_SH_PRODUCTS -> DWH.DIM_PRODUCT`: Type 2 historical versions.

Design decisions to freeze before implementation:

- Surrogate key strategy: sequence or identity.
- Natural key: `PROD_ID`.
- Required SCD2 columns: effective-from date, effective-to date, current flag,
  and audit columns.
- Type 1 current-state attribute list and Type 2 dimension change-detection attribute list.
- Implementation route: ODI SCD KM if assignable, otherwise an approved
  server-side expire-and-insert pattern.

Required pre-implementation evidence:

- Target metadata contract.
- Tracked attribute list.
- DQ gate list for date order, current flag values, and single current row per
  natural key.
- rerun guard definition for repeated same-change loads.
- rollback plan before any table create or migration.

## Phase 2 - Type 1 merge proof

Candidate target: one HR ODS table, such as `ODS_HR_DEPARTMENTS`.

Design decisions to freeze before implementation:

- Natural/update key.
- Change attributes.
- Insert-vs-update behavior.
- MERGE route: ODI IKM Merge or approved server-side MERGE.

Required pre-implementation evidence:

- Metadata-only key contract.
- Update/insert count expectations.
- Duplicate-prevention rule.
- rerun guard definition for same input.
- rollback plan for any target metadata or load-path change.

## Phase 3 - Partitioned daily fact load

Candidate target: `DWH.ODS_SH_SALES` or a new fact table.

Design decisions to freeze before implementation:

- Partition grain: daily or monthly.
- Migration route: new table plus backfill, CTAS/swap, or separate fact target.
- Window source: existing watermark/rerun guard.
- Rerun action for an already-loaded window.

Required pre-implementation evidence:

- Metadata-only table/partition contract.
- Window definition.
- Partition touch rule: only the target window partition should be affected.
- Count reconciliation plan.
- rollback plan before any partition migration.

## Phase 4 - Delete handling

Default policy recommendation: soft delete, not hard delete.

Design decisions to freeze before implementation:

- Delete detection method: count-only anti-join.
- Soft-delete columns and naming.
- Whether delete handling runs pre-load, post-load, or as a separate approved
  operator action.
- DQ and golden-record adjustments for soft-deleted records.

Required pre-implementation evidence:

- Count-only candidate-delete preflight.
- Approval gate shape.
- rerun guard definition for repeated delete apply.
- rollback plan for accidental marking.

## Phase 5 - Supporting patterns

Design notes only unless a separate issue or slice is approved:

- Late-arriving facts.
- Early-arriving dimensions.
- Inferred dimension members.
- Periodic snapshots.
- Aggregate or summary refresh per loaded window.

Required pre-implementation evidence:

- Business policy decision for each pattern.
- Metadata-only contract.
- DQ rule list.
- rollback plan.

## Acceptance Rules For Future Slices

Every implementation slice must state:

- Which phase it belongs to.
- Which action is read-only and which action is persistent.
- Which exact user approval is required before persistent action.
- Which count/status/metadata evidence will be captured.
- Which rerun guard prevents duplicates or repeated destructive work.
- Which rollback path exists before live DDL, DML, or ODI repository writes.

No implementation slice can be marked proven only because code exists. It needs
reviewed metadata evidence first, then separately approved live proof when a
live step is required.
