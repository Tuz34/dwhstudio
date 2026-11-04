# DIM_PRODUCT ODI SCD2 Capability Probe

## Purpose

This package defines a fixed-identity, metadata-only ODI SDK probe for the
future `DWH.ODS_SH_PRODUCTS` to `DWH.DIM_PRODUCT` Type 2 mapping.

## Safety Boundary

- The probe uses ODI finders and column role metadata only.
- It accepts only the configured project and folder. Datastore, mapping,
  scenario, IKM, and LKM identities are fixed in the script.
- It does not create or replace a datastore, mapping, scenario, or repository
  object.
- It does not generate or run a scenario.
- It returns no raw rows, PK values, repository IDs, expressions, KM option
  values, secrets, credentials, or connection material.
- Errors contain fixed text and an exception class name only.
- Live use remains behind the `ODI_REPOSITORY_READ_ONLY` approval gate.

## Normalized Evidence

The Python normalizer accepts an exact root schema and exact nested schemas for
capability, datastore metadata, SCD roles, keys, and object collisions. Any
extra field fails closed and the raw payload is not returned.

The result may say that the exact SCD2 IKM, LKM, target datastore metadata, or
role assignment is unavailable. That is a metadata finding, not permission to
change the repository.

## Next Boundary

A clean normalized result can only make the package ready for a separate
mapping-builder implementation review. It does not authorize mapping creation,
scenario generation, scenario execution, or replacement of the proven
server-side SCD2 executor.

