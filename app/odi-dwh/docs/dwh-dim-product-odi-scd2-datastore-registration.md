# DIM_PRODUCT ODI SCD2 Datastore Registration

This ODI-68 package defines one fixed, absent-only repository action for
`TGT_DWH.DIM_PRODUCT`. It exists because the reviewed live capability probe
found the physical DIM target but no matching ODI datastore metadata.

## Frozen boundary

- Model code: `TGT_DWH`.
- Target: `DWH.DIM_PRODUCT`.
- Target shape: 26 reviewed columns.
- Primary key metadata: `PK_DIM_PRODUCT` on `PRODUCT_SK`.
- Natural key metadata: `AK_DIM_PRODUCT_PROD_ID` on `PROD_ID`; it is semantic
  ODI metadata and is not declared as a physical database constraint because
  SCD2 history contains multiple versions of one natural key.
- SCD2 roles: surrogate key, natural key, start date, end date, current flag,
  and the 17 frozen tracked attributes.
- Existing target datastore behavior: block before mutation. Replacement,
  repair, merge, remove, and delete are not allowed.

The dedicated registrar accepts no caller-controlled model, datastore, column,
key, or role identity. It verifies the exact metadata before its only commit.
On failure it rolls back and returns fixed status plus an exception class only.

## Approval and review

The implementation requires a separate independent review and fresh exact
operator approval with scope
`odi68_dim_product_odi_scd2_datastore_registration`. A prior read-only probe,
DB create, mapping, scenario, or load approval cannot be replayed.

This package does not authorize mapping creation. It does not authorize scenario generation.
It does not authorize scenario run. A successful
registration must be followed by a fresh metadata-only capability probe before
the mapping builder can be considered.

Evidence is count/status/metadata only: no raw rows, no PK values, no repository
object IDs, no expressions, no KM option values, and no secret or connection
material.
