# DIM_PRODUCT SCD2 Candidate And Load Runbook

## Scope

This package contains two separately gated capabilities for ODI-68:

1. A read-only candidate preflight that classifies aggregate SCD2 work.
2. An expire then insert executor that remains disconnected from runtime wiring until independent review and exact operator approval.

No live SCD2 data load was run by this implementation slice.

## Read-Only Candidate Preflight

The preflight starts with `SET TRANSACTION READ ONLY`. It queries only the fixed objects `DWH.ODS_SH_PRODUCTS`, `DWH.DIM_PRODUCT`, and `DWH.ODI68_ODS_PRODUCTS_BKP`.

Evidence is count/status/metadata only. The report contains no raw rows and no PK values. It classifies each source row into exactly one aggregate group:

- new current version: the source natural key has no current target row;
- changed current version: the source natural key has one current target row and at least one tracked attribute differs;
- unchanged current version: the source and current target tracked attributes match.

The three classification counts must reconcile to the source count. Source duplicates, surrogate-key duplicates, multiple current rows, invalid flags, invalid effective dates, or an upstream backup mismatch fail closed.

A current target row missing from the source is a delete-policy blocker. Delete handling belongs to its own ODI-68 phase and cannot be silently treated as an SCD2 change.

Zero candidates produce `rerun_noop`. Non-zero candidates produce `ready_for_scd2_load_review`; this status does not authorize a write.

## Persistent Executor

The executor has no endpoint and no configured connection helper. It accepts only an injected connection factory. Before opening it, the executor requires:

- a structurally valid candidate report;
- executor review status exactly `approved`;
- literal boolean approval;
- exact scope `odi68_dim_product_scd2_load_and_rerun_proof`;
- a positive integer batch identifier.

The executor opens a serializable transaction, re-runs all candidate, DQ, and upstream backup counts, and blocks if any supplied evidence is stale.

The first pass expires only changed current versions, then inserts every source row that has no current target version. One database effective timestamp is reused for both operations. The insert uses the fixed `DWH.SEQ_DIM_PRODUCT` sequence.

Before commit, the executor verifies:

- expired and inserted counts match the reviewed candidates;
- target history count increased only by inserted versions;
- current target count equals source count;
- all source rows are unchanged after the load;
- candidate and DQ counts are clean.

It then repeats the same expire and insert statements as a same-transaction rerun. Both affected counts must be zero and the aggregate evidence must remain identical. Any mismatch or database error triggers rollback before commit and returns only a fixed error class name.

There is no destructive DDL, automatic object removal, or restore action in this executor. The existing ODS backup remains required and untouched.

## Live Boundary

Independent review covers the candidate query surface, fixed DML statements, transaction ordering, redaction, DQ checks, and rerun proof. Review completion does not inherit an earlier approval.

A future live load requires a fresh read-only preflight and a new exact approval for `odi68_dim_product_scd2_load_and_rerun_proof`. Candidate evidence cannot be replayed as authorization.
