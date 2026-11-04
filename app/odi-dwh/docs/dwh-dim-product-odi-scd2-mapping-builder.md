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
- `setXKM` for each physical access point, when an access point exists.

Same-location source and target designs may have zero access points. In that
case no LKM assignment is required. If access points exist, every one must have
the exact LKM before commit.

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

