# Phase 2 - Type 1 Merge Static Plan

This metadata-only plan refines `HR.DEPARTMENTS -> DWH.ODS_HR_DEPARTMENTS`. It does not authorize live work.

## Frozen contract

- Natural/update key: `DEPARTMENT_ID`.
- Tracked attributes: `DEPARTMENT_NAME`, `MANAGER_ID`, `LOCATION_ID`.
- Candidate route: server-side Type 1 merge, still subject to a separately reviewed executor slice.
- Evidence: inserted, refreshed, unchanged, duplicate, and total counts only.
- Rerun: the same input is a no-op and cannot add a duplicate natural key.

## Backup and approval

The current target must be preserved before persistent action. The restore path and return to the proven full-load path
must be reviewed. Metadata drift, missing backup evidence, missing read-only preflight, rerun rejection, or non-boolean
approval fails closed. Even all-ready static gates remain blocked pending a separate executor review.

## Safety boundary

- no raw rows; no PK values; no secrets or connection material.
- no executable statements, connection, ODI repository write, scenario run, or load.
- no persistent action without exact operator approval for that step.
