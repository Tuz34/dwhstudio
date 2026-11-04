# Real DIM_PRODUCT target and sequence executor

This ODI-68 slice prepares the fixed empty `DWH.DIM_PRODUCT` target and
`DWH.SEQ_DIM_PRODUCT` sequence. It is a separate persistent boundary from the
disposable proof and does not load SCD2 data.

## Required gates

- the reviewed 26-column static contract must be ready.
- independent review of this executor must be approved.
- literal approval and exact operator approval scope
  `odi68_dim_product_scd2_initial_create` are required.
- the disposable proof must have passed.
- a fresh read-only preflight must show the real target and sequence absent,
  aligned ODS metadata, and zero duplicate natural-key groups.
- the fixed ODS backup must remain present with its reviewed metadata/count.

Every gate is evaluated before the injected connection factory can be called.
There is no endpoint or configured connection helper.

## Fixed setup

- sequence: `DWH.SEQ_DIM_PRODUCT`, increment one, non-cycling.
- target: `DWH.DIM_PRODUCT`, 26 reviewed columns.
- target key: one primary-key constraint on `PRODUCT_SK`.
- initial target row count: zero.

The executor checks both object identities again immediately before setup. An
existing target or sequence blocks the rerun before persistent action.

## Oracle failure boundary

Oracle schema operations commit independently of caller rollback. Therefore the
executor performs no automatic cleanup of real objects. If a later setup or
verification stage fails, it reports whether the target or sequence may exist
and states that cleanup requires separate approval.

Errors expose fixed stage and class-name `error_type` only. Evidence is
count/status/metadata only, with no raw rows and no PK values.

## Current state

Live real-object setup has not been run. This implementation and its tests do
not authorize a live call. The executor must first pass independent review;
the exact operator approval is checked again at invocation time.

This slice does not load SCD2 rows, generate ODI mapping/scenario metadata, or
run an ODI scenario.
