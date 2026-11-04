# DIM_PRODUCT ODI SCD2 Scenario Package

## Generation Boundary

Scenario generation is fixed to:

- mapping: `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT`
- scenario: `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN`
- version: `001`
- approval scope: `odi68_dim_product_odi_scd2_scenario_generation`

Generation readiness requires an independently reviewed, read-only mapping
probe with one source component, one target component, 26 mapped target
attributes, `SCD` integration, the exact SCD2 IKM, and complete LKM assignment
for any access points. A separate read-only scenario probe must prove the exact
version is absent.

The wrapper then calls the existing absent-only scenario generator with fixed
identities. Its detailed checks are reduced to fixed scenario status metadata;
they are not returned to callers.

## Run-Readiness Boundary

Scenario execution has a different approval scope:

`odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof`

The connectionless run-readiness gate requires:

- the same exact mapping probe;
- a read-only probe proving scenario version `001` exists;
- a fresh count-only DIM_PRODUCT candidate preflight with `rerun_noop`;
- source, target, current, and unchanged counts equal;
- zero candidate, delete-policy, duplicate, flag, and effective-date findings;
- the fixed 24-column ODS backup present and unchanged.

Passing readiness does not run ODI. A separate fixed two-run executor must be
implemented and independently reviewed. That future executor must run the exact
scenario twice and collect count-only postflight evidence after each run.

No scenario generation or run was performed while preparing this package. No
raw rows, PK values, session IDs, repository IDs, expressions, secrets,
credentials, or connection material are included in its evidence.

