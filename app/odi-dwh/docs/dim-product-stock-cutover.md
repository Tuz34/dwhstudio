# Fixed stock cutover: new scenario 008

This operator-only code composes reviewed executors. It is not a new HTTP route
and importing it performs no IO. `execute_stock_cutover` and its configured
adapter default to `live=False`. No native acceptance is established by this code.

Before any factory or repository callback, require all of:

- Literal `live=True` and exclusive operator attestation for the entire operation.
- Separate literal approvals for the fixed stock checkpoint create, stable-audit
  repair, `odi87_dim_product_stock_scenario_generation`, and
  `odi87_dim_product_stock_scenario_run_and_rerun_proof` scopes.
- Independent checkpoint, audit, generator, runner and cutover reviews.
- A valid absolute receipt directory. No arbitrary target/model/version parameters.

The configured adapter also checks these gates before reading configuration or
instantiating its orchestrator. Runtime identities and connection settings are
never placed in its report. Existing scope approvals do not authorize another
scenario/version or target. Repository cleanup, scenario deletion and target
restore are not automatic.

## Sequence

1. Prove target topology `TGT_DWH` / `GLOBAL` / schema `DWH`. Retain the account
   internally only; unverified/ambiguous topology stops before any DB factory.
2. Verify the separate post-stock checkpoint. If absent, attempt only its reviewed
   absent-only creation. An existing mismatched object is not overwritten.
3. Require full 26-column equality with that retained checkpoint and count-only
   source/current business parity across the natural key and all 17 tracked fields.
   This fixed initial cutover requires 72 source/current/total rows and no history.
   Preserve the original legacy backups and original empty ODS backup.
4. Run a fresh stable-audit preflight. Only if needed, use the scoped receipt/CAS
   repair, then reread the stable profile. An already-stable mapping does not require
   a new repair or overwrite an existing receipt.
5. Probe the fixed mapping with no mutation: exact IKM, per-AP LKM, 26 mapped fields,
   SCD integration and all seven stock options. Incorrect options block, not repair.
6. Use the public absent-only generator for exactly
   `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN` version `008`. Require this invocation's
   verified new-generation result; existing `008` is not silently reused. Old `001`
   and historical Y/N/9999 evidence are not relabeled, deleted or regenerated.
7. Before each of two runs, refresh topology, full retained checkpoint/business
   parity and stable-audit profile. The existing remote runner independently checks
   exact scenario ownership before it contacts the agent.
8. After each acknowledged DONE, reread full checkpoint equality and business parity.
   Any changed business/audit/SK/date value fails proof. Stop before the second run
   when the first fails. Unknown transport is not retried and requires manual review.

The report distinguishes full persisted-snapshot equality from physical DML counts:
`physical_dml_count_proven` remains false. It does not infer zero executed UPDATEs
from identical final rows. Native generated SQL and unchanged-row audit behavior
must still be proven in the later backed-up live acceptance phase.

Oracle DDL and ODI repository/data transactions are not one atomic transaction.
Read-only witnesses do not lock out a future concurrent writer. The operator must
exclude concurrent sessions/editors throughout; partial/unknown results require
read-only reconciliation and separately authorized recovery, not blind retries.

Offline tests invoke the real Python operator wrappers with synthetic Groovy
reports and SQLite-backed aggregate/full-row proofs. Separate fake-SDK tests run
the real probe and validate every stock option without a repository connection.
