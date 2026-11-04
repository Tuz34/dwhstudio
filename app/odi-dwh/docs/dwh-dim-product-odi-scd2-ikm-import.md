# DIM_PRODUCT SCD2 IKM Import Boundary

## Purpose

This package prepares one absent-only repository import of the official Oracle
SDK reference `IKM Oracle Slowly Changing Dimension`.

The implementation is fixed to:

- file name: `KM_IKM Oracle Slowly Changing Dimension.xml`
- target technology: `ORACLE`
- integration type: `SLOWLY_CHANGING_DIMENSION`
- import mode: `DUPLICATION`
- approval scope: `odi68_dim_product_scd2_ikm_import`

The Oracle home is supplied by the local application configuration. No API or
operator input can select another XML file, KM name, import mode, or technology.

## Fail-Closed Gates

Before the importer can open a repository connection, a fresh normalized
capability probe must prove:

- the 24-column source and 26-column target datastore contracts are exact;
- the target key and SCD role metadata are exact;
- the required LKM and SCD role API are available;
- the exact SCD2 IKM is absent;
- the fixed mapping and scenario are absent;
- the probe, contract, and importer implementation reviews are approved;
- literal `approved is True` and the exact approval scope are present.

If the IKM already exists, the importer stops. There is no update, synonym,
replace, remove, or caller-selected import path.

## Runtime Boundary

The Groovy implementation uses Oracle's `ImportServiceImpl` with
`IMPORT_MODE_DUPLICATION`, then reads the imported object back and verifies its
fixed name, target technology, and integration type.

The Oracle import service owns its transaction boundary. If an import call was
attempted and a later verification fails, the error result honestly marks that
repository state may have changed. Automatic cleanup is not attempted; recovery
requires a separate reviewed action and approval.

## Evidence

Normalized evidence contains only status, fixed identity, booleans, and the
class-only `error_type`. It contains no XML path, repository ID, KM body, option
value, raw row, PK value, secret, credential, or connection material.

This document does not authorize the live import. Independent review and a
fresh exact approval remain required at execution time.

