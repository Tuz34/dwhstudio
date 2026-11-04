# PRODUCTS ODS Scenario Readiness

This contract prepares scenario metadata generation without opening a database
or ODI repository connection.

## Fixed identity

- Mapping: `MAP_SH_PRODUCTS_TO_ODS`
- Scenario: `MAP_SH_PRODUCTS_TO_ODS_SCN`
- Version: `001`
- Approval scope: `odi68_products_ods_scenario_generation`

## Required gates

1. The live mapping slice has an independent approved review.
2. The read-only mapping inspection reports `INCREMENTAL_UPDATE`.
3. The update key contains exactly `PROD_ID`.
4. The target IKM is `IKM Oracle Merge`.
5. The AP LKM is `LKM SQL to Oracle (Built-In)` and is not a DB-Link LKM.
6. Both ODS audit attributes use `CURRENT_TIMESTAMP`.
7. A read-only scenario probe confirms that the exact scenario version is absent.
8. Approval is the literal boolean `true` and the exact scope matches.

Even when every gate passes, the static readiness builder does not generate or
run a scenario. It only authorizes invoking the separately reviewed absent-only
generator. Scenario execution remains a different approval boundary.

## Legacy-path hardening

Active Python callers no longer use the destructive legacy Groovy generator.
They route through `generate_scenario_metadata.groovy`, which leaves existing
scenario versions in place, uses the ODI cast-safe generation path, and returns
fixed failure text plus an exception class only.

## Evidence boundary

The readiness result contains identifiers, statuses, and mapping metadata only.
It contains no raw rows, PK values, secrets, credentials, connection material,
scenario output, or execution claims.
