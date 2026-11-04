# PRODUCTS ODS Scenario Run Executor

This executor is the reviewed orchestration boundary for proving the first run
and rerun behavior of `MAP_SH_PRODUCTS_TO_ODS_SCN` version `001` in `GLOBAL`.
It has no endpoint, configured connection helper, or direct ODI/Oracle import.

## Required gates

The executor accepts only the exact output of the PRODUCTS scenario
run-readiness contract. That contract already requires the reviewed mapping and
scenario metadata, consistent count-only candidate evidence, a verified named
backup, and separate exact approval scope
`odi68_products_ods_scenario_run_and_rerun_proof`.

The executor itself also requires an independent `approved` review verdict
before either injected callback can run.

## Proof sequence

1. Invoke the exact scenario once.
2. Require a successful `DONE` result that contains no raw runtime output or
   session identifier.
3. Run a count-only postflight. Source and target counts must match, duplicate
   groups must be zero, insert/update candidates must be zero, and all source
   records must be unchanged.
4. Invoke the exact scenario twice in total.
5. Repeat the count-only postflight and require the same clean target count.

Any failed run or postflight stops the sequence. The named backup remains. No automatic rollback or restore is attempted because Oracle/ODI commits are
outside one caller-controlled transaction. A restore requires separate review
and separate exact approval.

## Current evidence boundary

No live scenario run was performed by this slice. Tests use injected fake
runner and probe callbacks only. Returned evidence contains statuses and counts,
with no raw rows, no PK values, no runtime output, no session identifier, no
secret, and no connection material.
