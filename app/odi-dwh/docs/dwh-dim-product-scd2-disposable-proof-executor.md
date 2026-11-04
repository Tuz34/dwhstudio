# DIM_PRODUCT SCD2 disposable target and sequence proof executor

This ODI-68 slice provides a narrowly bounded executor for fixed proof objects.
It does not expose the real DIM target or real sequence to a cleanup path.

## Safety boundary

- fixed proof objects only.
- independent review is required before invocation.
- literal approval and exact operator approval scope
  `odi68_dim_product_scd2_disposable_create_cleanup` are required.
- no configured connection helper or endpoint exists.
- evidence is count/status/metadata only.
- no raw rows.
- no PK values.
- no secret, credential, or connection material.
- live proof has not been run.

## Fixed proof objects

- table: `DWH.ODI68_DIM_PRODUCT_PROOF`
- sequence: `DWH.ODI68_SEQ_DIM_PRODUCT_PROOF`

The table uses the reviewed 26-column DIM contract and a primary-key constraint
on `PRODUCT_SK`. The sequence must increment by one and must not cycle.

The executor blocks if either proof object already exists. It never removes a
pre-existing object. Cleanup is limited to proof objects created by the same approved call.

## Proof order

1. Confirm both fixed proof objects are absent.
2. Prepare the disposable sequence and table.
3. Verify 26-column metadata, one `PRODUCT_SK` constraint, zero rows, sequence
   increment, and non-cycling metadata.
4. Remove the disposable table and sequence.
5. Confirm both proof objects are absent.

Oracle schema operations commit outside normal caller rollback. A failed cleanup
therefore reports the remaining proof-object status without claiming automatic
rollback. The real `DWH.DIM_PRODUCT` and `DWH.SEQ_DIM_PRODUCT` identities are not
accepted by any removal function in this module.

## Not authorized

This slice does not authorize the disposable live run, real target setup, SCD2
data loading, mapping generation, or scenario execution. The disposable run and
the later real setup require separate review and separate exact approvals.
