# PRODUCTS ODS Initial-Create Executor

This slice prepares an endpoint-free executor for the reviewed `DWH.ODS_SH_PRODUCTS` initial target. The reviewed live
disposable DDL proof passed on 2026-07-13, followed by the separately approved creation of the empty real target.

## Safety sequence

1. The static target contract must be ready.
2. The executor itself must have independent review status `approved`.
3. The operator must provide `approved: true` and the exact operator approval scope for the selected action.
4. A disposable create/cleanup proof must pass before the real target path can open a connection.
5. The real target rerun guard blocks when the target already exists.

The executor uses a fixed table allowlist containing only the real target and one disposable proof object. It has no
endpoint and cannot accept an owner or table from a client request.

## Disposable proof

The disposable create/cleanup proof prepares an empty object with the reviewed 24-column metadata and unique `PROD_ID`
contract, verifies metadata/count/status, removes only the object created by that proof call, and confirms absence by
metadata. It does not load source rows.

## Oracle transaction boundary

Oracle DDL auto-commits. The disposable object can be removed automatically because its fixed name is checked as absent
first and the executor tracks that it was created by the current call. The real target is never removed automatically;
if post-create verification fails, cleanup requires a separate reviewed action and separate approval.

## Evidence boundary

- count/status/metadata only.
- no raw rows.
- no PK values.
- no secrets or connection material.
- no endpoint exposure.
- no Type 1 data load in this slice.
- live disposable DDL proof passed; the proof object was removed and confirmed absent.
- the real target was created empty and no Type 1 source-row load was run.
