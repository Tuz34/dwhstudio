# DIM_PRODUCT SCD2 Attribute Change Rollback Proof

## Scope

This ODI-68 package proves one tracked-attribute SCD2 transition inside a
rollback-only database transaction. It does not create or run an ODI mapping,
package, scenario, or load plan. No live proof was run by this implementation
slice.

The proof has no endpoint and no configured connection helper. It accepts only
an injected connection factory.

## Required Baseline

Before opening a connection, the proof requires:

- a fresh read-only candidate preflight with status `rerun_noop`;
- executor review status exactly `approved`;
- literal boolean approval;
- exact scope `odi68_dim_product_scd2_attribute_change_rollback_proof`;
- a positive integer batch identifier.

The proof starts a serializable transaction and re-runs the aggregate candidate
evidence before any write. A changed baseline blocks the proof.

## Rollback-Only Transition

The source simulation updates exactly one deterministic
`DWH.ODS_SH_PRODUCTS` row and toggles the tracked `PROD_STATUS` attribute. The
natural-key value is selected inside the fixed statement and is never returned
or included in the report.

The proof requires exactly one changed candidate, expires exactly one current
`DWH.DIM_PRODUCT` version, and inserts exactly one replacement version. It then
repeats the expire and insert statements in the same transaction. Rerun
affected counts must both be zero and all aggregate evidence must remain
unchanged.

The proof insert deliberately does not call `DWH.SEQ_DIM_PRODUCT.NEXTVAL`.
Oracle sequence advancement is not transactional, so the rollback-only proof
uses `MAX(PRODUCT_SK) + 1` for its single temporary row. The production load
executor remains unchanged and continues to use the real sequence.

## Safety Boundary

Rollback is mandatory on success, blocked state, or error. The proof contains
no commit, DDL, delete, truncate, automatic cleanup, endpoint, or configured
runtime wiring. A rollback failure returns `failed_rollback` and does not claim
that persistent state was preserved.

Evidence is count/status/metadata only. It contains no raw rows, PK values,
batch identifier value, secrets, SQL text, or exception message.

## Live Boundary

Independent review covers the fixed one-row simulation, production expire
statement reuse, rollback-safe insert, same-transaction rerun, mandatory
rollback, and redaction. Review completion does not authorize a live proof.

A future live proof requires a fresh `rerun_noop` preflight and a new exact
operator approval for
`odi68_dim_product_scd2_attribute_change_rollback_proof`. After the proof, a
separate read-only postflight must confirm that source, target, current-row,
candidate, DQ, and backup counts are unchanged.
