# DIM_PRODUCT ODI SCD2 Packaging Readiness

## Decision

The proven server-side executor remains the operational baseline for
`DWH.DIM_PRODUCT`. ODI packaging is an additional operability path and cannot
replace that baseline until it is proven side by side.

The existing generic Type 1 builder is not reusable for this job. It assigns
incremental-update semantics, an update key, and `IKM Oracle Merge`. It does not
assign the frozen SCD2 column roles or require
`IKM Oracle Slowly Changing Dimension`.

## Intended ODI Objects

- Source datastore: `DWH.ODS_SH_PRODUCTS`
- Target datastore: `DWH.DIM_PRODUCT`
- Mapping: `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT`
- Scenario: `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN`, version `001`
- Target KM: `IKM Oracle Slowly Changing Dimension`
- LKM: `LKM SQL to Oracle (Built-In)`

The contract freezes `PRODUCT_SK` as the surrogate key, `PROD_ID` as the
natural key, `EFFECTIVE_FROM` and `EFFECTIVE_TO` as the version dates,
`CURRENT_FLAG` as the current-row marker, and the same 17 tracked attributes as
the proven server-side implementation.

## Required Read-Only Evidence

A separate read-only ODI capability probe must normalize only:

- whether the exact target IKM is installed and assignable for Oracle;
- whether the SCD2 role-assignment API is supported;
- fixed source and target datastore identities and column counts;
- normalized key and SCD role column names;
- absence or presence of the exact mapping and scenario identities.

Evidence is count/status/metadata only. It must not return raw rows, PK values,
column expressions, KM option values, repository object IDs, secrets,
credentials, or connection material.

Missing, ambiguous, side-effecting, mismatched, or collision evidence fails
closed. Readiness does not authorize a repository write.

## Separate Persistent Boundaries

After the read-only probe is independently reviewed and passes, the next code
slice is a separate mapping-builder implementation. It must be absent-only,
fixed-identity, dedicated to this SCD2 contract, and independently reviewed.
Mapping creation then requires exact mapping creation approval.

The created mapping must be inspected read-only before any scenario work.
Scenario generation needs its own implementation, independent review, and
separate scenario generation approval. A scenario run needs another fresh
readiness check and separate scenario run approval.

## Current State

No ODI connection was opened by this slice. No ODI object was created or run.
No DB write, ODI repository write, Groovy execution, or model inference
occurred.
