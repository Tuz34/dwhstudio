# DIM_PRODUCT ODI SCD2 IKM Resolution

The installed ODI SDK contains one allowlisted Oracle Type 2 reference:
`KM_IKM Oracle Slowly Changing Dimension.xml`. Structured offline inspection
confirms only these identity fields:

- name: `IKM Oracle Slowly Changing Dimension`;
- technology: `ORACLE`;
- integration type: `SLOWLY_CHANGING_DIMENSION`.

The earlier reviewed live capability evidence found that this exact IKM is not
installed in the current ODI repository. The installed SDK template and the
repository object are different states: a local reference file does not make a
KM assignable to a mapping.

This package is connectionless and read-only. It returns allowlisted metadata
only: no raw rows, no PK values, no repository IDs, no GlobalId, no KM options,
no implementation text, and no secret or connection material.

## Next boundary

Repository installation requires a separate import implementation, a separate independent review,
and exact operator approval. This resolution package does
not authorize that repository write. It does not authorize mapping creation.
It does not authorize scenario run. After any reviewed import, a fresh
metadata-only capability probe must confirm the exact IKM and Oracle target
technology before mapping work can continue.
