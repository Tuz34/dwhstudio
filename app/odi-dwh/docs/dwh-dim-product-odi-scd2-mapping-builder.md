# DIM_PRODUCT ODI SCD2 Mapping Builder

## Fixed Contract

The dedicated builder can create only the absent mapping
`MAP_ODS_PRODUCTS_TO_DIM_PRODUCT` in the configured ODI project folder.

- source datastore: `TGT_DWH.ODS_SH_PRODUCTS`
- target datastore: `TGT_DWH.DIM_PRODUCT`
- integration type: `SCD`
- target IKM: `IKM Oracle Slowly Changing Dimension`
- optional access-point LKM: `LKM SQL to Oracle (Built-In)`
- approval scope: `odi68_dim_product_odi_scd2_mapping_creation`

The generic Type 1 mapping builder is not reused.

## Mapping Rules

The builder verifies the exact 24/26-column datastores and frozen SCD roles
before creating anything. It maps the natural key and 17 tracked attributes
from ODS. Fixed target-side expressions cover the surrogate key sequence,
effective dates, current flag, audit timestamps, source system, and batch
metadata. The surrogate expression is explicitly executed on the target.

Only public ODI SDK KM assignment APIs are used:

- `setTargetComponentKM` for the fixed SCD2 IKM;
- `setLKM` and `getLKMName` for each physical access point, when one exists.

An access point does not use the XKM accessors. In the installed SDK they read a
separate extract reference, which is empty on an access point.

Same-location source and target designs may have zero access points. In that
case no LKM assignment is required. If access points exist, every one must have
the exact LKM before commit.

## Commit Gates

The writer now checks these before it commits, and rolls back if any fails:

- Each KM needs exactly one exact-name candidate that is global or owned by the
  configured project. No candidate, or more than one, blocks before the mapping
  is created.
- `FLOW_CONTROL` and `STATIC_CONTROL` are written as false, so no CKM runs. The
  builder does not choose a CKM.
- `RECYCLE_ERRORS`, `TRUNCATE`, `DELETE_ALL` and `CREATE_TARG_TABLE` must read
  false and `COMMIT` must read true. These are read back and never written. A
  missing option or a different value blocks.
- A first `validate(issues, false)` lets the SDK settle the design. Its result is
  not a gate, because it can describe a state the builder then repairs. Options
  and access-point LKMs are written again after it.
- The final `validate(issues, false)` blocks on a true result, including a true
  result with no issue, and on any `ERROR` or `FATAL` issue. Warnings do not
  block. An exception blocks.
- The IKM, options, LKMs and access-point count are read again after that final
  call.

Issue text and exception text are never output. An error returns a class name
only.

## What This Does Not Fix

- **Audit columns.** `INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM` and
  `ETL_BATCH_ID` have no SCD role on the datastore. The stock IKM fills `I$` by
  SCD role and its final insert reads every target column from `I$`, so the
  mapping expressions do not reach these columns. No mapping option changes
  this. The fix is a role change on existing datastore metadata, which this
  absent-only builder does not make. That repair stays with the coordinator. A
  mapping created by this builder must not be treated as populating audit columns
  until that repair is proven.
- **Existing mappings.** An existing mapping still blocks. This builder does not
  repair or replace one, so a deployed mapping keeps its old KM and options.
- **Scenario version.** The pin stays at `001`. No scenario is generated or run.
- **Read-only probe.** `dim_product_scd2_mapping_probe.groovy` still counts
  assigned LKMs through `getXKMName`. It is outside this change and can
  under-count access-point LKMs.
- **Commit acknowledgement.** A commit failure still reports `side_effects:
  false`, as the result normalizer requires, although the outcome may be unknown.
- **Native proof.** The evidence is a fake SDK plus public API checks against the
  installed jar. The boolean meaning of `validate` comes from the jar's bytecode
  and earlier generic builder notes. No native ODI validation, mapping creation
  or scenario run has tested this code.

## Safety Boundary

The writer is absent-only. An existing mapping or fixed scenario blocks before
creation. It has no replacement, remove, reflection fallback, scenario
generation, scenario run, target DDL, or data-load surface. A failed transaction
is rolled back and returns fixed text plus a class-only error type.

The separate read-only mapping probe returns only component, attribute, access
point, and assigned-KM counts plus fixed names. It does not return expressions,
repository IDs, raw rows, PK values, secrets, credentials, or connection
material.

This package does not authorize live mapping creation. Independent review, a
fresh capability probe after IKM import, and the exact approval scope are still
required.

