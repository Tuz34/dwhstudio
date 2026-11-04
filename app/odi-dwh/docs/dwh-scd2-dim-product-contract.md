# Phase 1 - Type 2 SCD DIM_PRODUCT metadata contract

This is a non-executable metadata contract for ODI-68 Phase 1:
`DWH.ODS_SH_PRODUCTS -> DWH.DIM_PRODUCT`.

The reviewed upstream step is `SH.PRODUCTS -> DWH.ODS_SH_PRODUCTS` using Type 1 current-state synchronization.

It defines the review boundary for a future Type 2 SCD implementation slice. It
does not authorize live work and does not contain runnable SQL.

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

A later live proof must use the [fixed DIM_PRODUCT scenario-run executor](../dim_product_odi_scd2_scenario_run_executor.py) with exact scope `odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof`; this Phase 1 contract grants no live-run approval.

## Target Metadata Contract

Candidate target: `DWH.DIM_PRODUCT`.

Candidate source: `DWH.ODS_SH_PRODUCTS` after its Type 1 gate is proven.

Column families to freeze before any live action:

- surrogate key: generated dimension key for the target row version.
- natural key column: `PROD_ID`.
- effective-from: start timestamp or date for the row version.
- effective-to: end timestamp or date for the row version.
- current flag: single-character current-row marker.
- audit columns: load timestamp, source system marker, and batch/window marker.
- tracked attribute list: product descriptive attributes selected by metadata
  review before implementation.

The exact physical data types and names must be finalized in a later reviewed
implementation slice before any live schema work.

## Tracked Attribute List

The first implementation slice must explicitly list which product attributes are
tracked for Type 2 history. The list must be derived from metadata, not raw row
inspection.

Minimum rules:

- Include only business attributes whose history should be retained.
- Exclude technical audit columns from change detection.
- Exclude generated surrogate key and effective dating columns from change
  detection.
- Treat the tracked attribute list as a reviewed contract; changing it later
  requires its own review note.

## DQ Gate List

The future post-load gate must include these metadata-level rules:

- date order: effective-from must not be after effective-to.
- current flag values: only the approved current and historical markers are
  allowed.
- single current row per natural key: at most one current row exists for each
  natural key column value.
- no open-ended historical overlap for the same natural key column.
- row-count movement is explained with count/status/metadata evidence only.

## Rerun Guard

The future implementation must define a rerun guard before any persistent action.

Required behavior:

- Repeating the same no-change input must be a no-op by metadata count.
- Repeating the same detected attribute change must not create duplicate current
  versions.
- A blocked or already-applied window must return a status result before any
  persistent action is attempted.
- The guard result must be visible in the Evidence Pack as status/metadata, not
  row samples.

## Approval Gate

The future implementation must separate read-only preparation from persistent
action.

Required approval shape:

- Read-only preflight may collect metadata counts and status.
- Persistent schema or load behavior requires explicit operator approval.
- Approval cannot be inferred from this document, from issue status, or from a
  previous phase.
- A reviewer must be able to see which action is read-only and which action is
  persistent before the operator approval is requested.

## Rollback Plan

A rollback plan is required before any future schema or load path change.

Minimum rollback requirements:

- State how the previous target state is preserved before the first persistent
  action.
- State how an unapproved or failed SCD2 apply step stops before partial
  progression.
- State how DQ failure prevents commit or marks the run as blocked before it is
  treated as proven.
- State how the operator can return to the proven full-load path while the SCD2
  path remains under review.

## Review Checklist For The Next Slice

The next implementation slice must return for review with:

- finalized target metadata contract,
- finalized tracked attribute list,
- DQ gate implementation plan,
- rerun guard implementation plan,
- rollback plan,
- approval gate text,
- count/status/metadata-only evidence plan.

No future slice may claim proof from code presence alone. It needs reviewed
metadata evidence first, then separately approved live proof if live work is
required.
