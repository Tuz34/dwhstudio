# PRODUCTS ODS Scenario Run Readiness

This static contract prepares, but does not perform, the first live run of
`MAP_SH_PRODUCTS_TO_ODS_SCN` version `001` in the `GLOBAL` context.

## Required evidence

The gate requires all of the following before a separate run executor can be
considered:

1. The scenario-generation slice has an independent approved review.
2. A read-only mapping inspection still matches the reviewed Type 1 contract.
3. A read-only scenario probe finds the exact scenario and version.
4. The Type 1 candidate preflight is count-only, internally consistent, and
   reports no duplicate natural-key groups.
5. The fixed named backup exists, has 24 columns, and its row count matches the
   reviewed target count. The disposable restore proof must be cleaned up.
6. Approval is the literal boolean `true` and uses the exact operator approval
   scope `odi68_products_ods_scenario_run_and_rerun_proof`.

## Commit and recovery boundary

Oracle/ODI commits occur inside the scenario execution path. A scenario run
therefore has no automatic rollback equivalent to the server-side Type 1
executor's single transaction. The named backup remains the recovery source.
Any restore action requires its own reviewed implementation and exact approval.

The future separate run executor must run count-only postflight checks after
the first execution, run the exact scenario a second time, and prove that the
second run is a no-op. A failure after an ODI commit must stop the sequence and
report that manual recovery review is required; it must not silently restore or
drop objects.

## Current evidence boundary

No scenario run was performed by this slice. It opens no repository or database
connection and contains no run wrapper. Evidence is limited to status, counts,
and reviewed metadata. It includes no raw rows, no PK values, no secrets, and no
connection material.
