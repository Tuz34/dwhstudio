# DIM_PRODUCT ODI SCD2 Mapping Builder Contract

## Purpose

This static contract freezes the boundary for a future dedicated
`MAP_ODS_PRODUCTS_TO_DIM_PRODUCT` builder.

## Frozen Rules

- Source: `DWH.ODS_SH_PRODUCTS`.
- Target: `DWH.DIM_PRODUCT`.
- Target IKM: `IKM Oracle Slowly Changing Dimension`.
- LKM: `LKM SQL to Oracle (Built-In)`.
- The mapping may be created only when the exact mapping is absent.
- The generic Type 1 merge builder is not reusable.
- Mapping replacement is not allowed.
- Scenario generation and scenario execution are separate packages.
- The proven server-side SCD2 executor remains the operational baseline.

## Gates

The future builder requires reviewed packaging readiness, an approved
capability probe, an approved builder contract, literal operator approval, and
the exact scope `odi68_dim_product_odi_scd2_mapping_creation`.

Even when all gates pass, this package returns
`blocked_pending_separate_mapping_builder_implementation`. It contains no
connection, Groovy invocation, endpoint, repository writer, or executable
builder.

## Safety Boundary

This contract does not authorize mapping creation. A dedicated implementation
must receive a separate review and fresh exact approval. No raw rows, PK values,
repository IDs, expressions, KM option values, secrets, credentials, or
connection material are included.

