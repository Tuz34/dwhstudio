# Phase 2 - Type 1 merge metadata contract

This is a non-executable metadata contract for ODI-68 Phase 2:
`HR.DEPARTMENTS -> DWH.ODS_HR_DEPARTMENTS`.

It defines the review boundary for a future Type 1 (upsert/merge) load
implementation slice. It does not authorize live work and does not contain
runnable SQL.

## Safety Boundary

- count/status/metadata only.
- no raw rows.
- no PK values.
- no secrets or credential material.
- no DDL.
- no DML.
- no ODI scenario run.
- no WebLogic, Docker, Task Scheduler, or service change.
- no Git or Gitea workflow.
- every persistent action requires explicit operator approval in a later slice.

## Target Metadata Contract

Candidate target: `DWH.ODS_HR_DEPARTMENTS`.

Candidate source: `HR.DEPARTMENTS`.

Column families to freeze before any live action:

- natural key column: `DEPARTMENT_ID`.
- update-key: the same natural key column used to locate the existing target
  row for an insert-or-refresh choice.
- audit columns: load timestamp, source system marker, and batch/window
  marker.
- tracked attribute list: department descriptive attributes selected by
  metadata review before implementation.

The exact physical data types and names must be finalized in a later reviewed
implementation slice before any live schema work.

## Natural Key, Update-Key, and Insert-vs-Refresh Behavior

The natural key and update-key are the same column for this Phase 2 target:
`DEPARTMENT_ID`.

Minimum rules:

- If a source row's natural key value is not present in the target, the
  future implementation must treat it as an insert-candidate.
- If a source row's natural key value is present in the target, the future
  implementation must treat it as an update-candidate and must not create a
  duplicate row for that natural key.
- Insert-vs-refresh classification must be derived from natural key presence
  in metadata/count evidence, not from raw row inspection.
- The MERGE route (ODI IKM Merge or an approved server-side MERGE) must be
  selected in the next implementation slice and stated explicitly before any
  live execution.

## Tracked Attribute List

The first implementation slice must explicitly list which department
attributes are tracked for Type 1 change detection. The list must be derived
from metadata, not raw row inspection.

Minimum rules:

- Include only business attributes whose current value should be kept in
  sync with the source.
- Exclude technical audit columns from change detection.
- Exclude the natural key and update-key columns from change detection.
- Treat the tracked attribute list as a reviewed contract; changing it later
  requires its own review note.

## DQ Gate List

The future post-load gate must include these metadata-level rules for a
Type 1 merge:

- no duplicate natural key in target: at most one row exists in the target
  for each natural key column value.
- refresh-vs-insert count reconciliation: the future implementation must
  report insert-count, update-count, and unchanged-count as count/status
  evidence only.
- required columns present: natural key and tracked attribute columns must
  be present and non-null according to the reviewed contract before the
  merge is considered evidence-complete.
- row-count movement is explained with count/status/metadata evidence only.

## Rerun Guard

The future implementation must define a rerun guard before any persistent
action.

Required behavior:

- Repeating the same no-change input must be a metadata no-op: the rerun
  guard must report zero net row-count change and zero new inserts.
- Repeating the same input must not create duplicates: the natural key
  uniqueness rule must hold before and after any rerun.
- A blocked or already-applied window must return a status result before any
  persistent action is attempted.
- The guard result must be visible in the Evidence Pack as status/metadata,
  not row samples.

## Approval Gate

The future implementation must separate read-only preparation from
persistent action.

Required approval shape:

- Read-only preflight may collect metadata counts and status.
- Persistent schema or load behavior requires explicit operator approval.
- Approval cannot be inferred from this document, from issue status, or from
  a previous phase.
- A reviewer must be able to see which action is read-only and which action
  is persistent before the operator approval is requested.

## Rollback Plan

A rollback plan is required before any future schema or load path change.

Minimum rollback requirements:

- State how the previous target state is preserved before the first
  persistent action.
- State how an unapproved or failed Type 1 merge apply step stops before
  partial progression.
- State how DQ failure prevents commit or marks the run as blocked before it
  is treated as proven.
- State how the operator can return to the proven full-load path while the
  Type 1 merge path remains under review.

## Review Checklist For The Next Slice

The next implementation slice must return for review with:

- finalized target metadata contract,
- finalized tracked attribute list,
- finalized natural key and update-key contract,
- finalized insert-vs-refresh behavior and MERGE route,
- DQ gate implementation plan,
- rerun guard implementation plan,
- rollback plan,
- approval gate text,
- count/status/metadata-only evidence plan.

No future slice may claim proof from code presence alone. It needs reviewed
metadata evidence first, then separately approved live proof if live work is
required.
