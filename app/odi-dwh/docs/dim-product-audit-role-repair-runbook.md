# DIM_PRODUCT audit-role repair runbook

**Status: code only.** No part of this has run against a real ODI repository. This runbook
does not authorize a live repair, and it does not complete the DIM_PRODUCT cutover.

## Why this exists

`INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM` and `ETL_BATCH_ID` on the fixed `DIM_PRODUCT`
datastore have no SCD role. The stock IKM fills `I$` by SCD role, and its final insert reads
every target column from `I$`, so these four columns reach the target as NULL. No mapping
option changes that. This package sets the role of these four columns to
`OVERWRITE_ON_CHANGE`, using the public `OdiColumn.setScdType`.

## What changes after a repair

Read this before approving a repair. It comes from reading the stock IKM tasks, not from a run.

- **Every load rewrites matched current rows.** Task `Flag rows for update` marks a row for
  update when any overwrite-role column differs. `UPDATE_DATE`, `INSERT_DATE` and
  `ETL_BATCH_ID` differ on each run, so unchanged current rows are updated each time.
- **`INSERT_DATE` stops meaning first insert.** On a matched row it becomes the last load
  time. `docs/dwh-scd2-generic-builder.md` documents the same behaviour for generic targets.
- **A rerun cannot be a full-snapshot or physical no-op.** Business columns stay equal, but
  the audit columns change. A check that requires an unchanged audit snapshot will fail.
- **Any existing scenario becomes stale.** It was generated from the old roles. This package
  does not delete, generate or run a scenario. A new version needs its own review.

## Scope

Fixed and not caller-controlled: model `TGT_DWH`, datastores `DIM_PRODUCT` and
`ODS_SH_PRODUCTS`, mapping `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT`, and the four audit columns. The
project and folder come from server configuration and must match exactly, including case.

It changes no database row, no other column, no mapping and no scenario.

## Entry point

`OdiOrchestrator.dim_product_audit_role_action(action=...)`, where `action` is `preflight`,
`repair` or `restore`.

| Action | Exact scope | Also requires |
| --- | --- | --- |
| `preflight` | `odi87_dim_product_audit_role_preflight` | `approved=True` |
| `repair` | `odi87_dim_product_audit_role_repair` | `approved=True`, review, attestation, receipt directory |
| `restore` | `odi87_dim_product_audit_role_restore` | `approved=True`, review, attestation, receipt directory |

- Review means `implementation_review_status="approved"`.
- Attestation means `no_active_odi_session_writes_attested=True`. An operator states this. The
  script separately refuses when running, waiting or queued ODI sessions exist, but it cannot
  see someone editing metadata in ODI Studio.
- The receipt directory must be an existing absolute path chosen by server code.

A failed gate makes no script call. The script checks action, approval and scope again before
it connects.

No proof is taken from a caller. `repair` runs its own preflight first, and the script checks
everything again inside the write transaction.

## What the script checks

The same check runs before the write and again after flush, before commit:

- exactly one project, folder, model, source datastore, target datastore and mapping;
- Oracle technology on the model;
- 26 target columns and 24 source columns, with the exact target names;
- surrogate key, natural key, start, end and current-flag roles, and all 17 tracked columns as
  `ADD_ROW_ON_CHANGE`;
- each audit role is NULL or `OVERWRITE_ON_CHANGE`;
- the mapping has exactly the fixed source and target components, in `TGT_DWH`, as SCD;
- all 26 target attributes have an expression, and the four audit expressions match exactly.

Expression text is compared inside the script and never output.

**Not checked:** the text of the other 22 expressions, only that each exists. A deployed mapping
that still has older flag or end-date expressions passes this check and needs separate handling.

## Reports

| `status` | `repository_outcome` | `side_effects` | Manual review |
| --- | --- | --- | --- |
| `ready_for_repair`, `already_repaired`, `already_restored` | `no_write` | `false` | no |
| `repaired`, `restored` | `committed` | `true` | no |
| `blocked`, `aborted` | `rolled_back` or `no_transaction` | `false` | no |
| `outcome_unknown` | `rollback_failed` | `null` | yes |
| `outcome_unknown` | `commit_outcome_unknown` | `null` | yes |
| `committed_report_incomplete` | `committed` | `true` | yes |
| `outcome_unknown` | `unverified` | `null` | yes |

- A commit that raised stays unknown even after a successful rollback.
- If a write call returns no usable report, including a timeout or non-zero exit, the result is
  `outcome_unknown`, never proof that nothing changed.
- Reports hold fixed codes and class names only, plus column names, role names and counts.

## Receipts

Three files with fixed names, each created once and never overwritten:

| File | Written | Holds |
| --- | --- | --- |
| `odi87_dim_product_audit_role_receipt.json` | Before the repair call | Before image, target image, changed columns, fixed object names |
| `odi87_dim_product_audit_role_outcome.json` | After the repair call | Status and repository outcome |
| `odi87_dim_product_audit_role_restore.json` | After a restore that was not blocked | Status and repository outcome |

An existing receipt blocks a new repair. The digest detects accidental damage. It is not a
signature, and it does not stop someone who can write to the directory.

## Restore

Restore never runs by itself. It needs its own scope, and it reverts only the columns the receipt
lists as changed, back to NULL.

| Situation | Result |
| --- | --- |
| Receipt missing, damaged or edited | Blocked before any call |
| Restore marker already exists | Blocked as stale |
| Outcome shows nothing was applied | Blocked, nothing to restore |
| Outcome unknown or missing | Allowed only through the state check below |

Inside its transaction the script accepts only two states for the four columns:

- **The exact repaired state:** it reverts the listed columns and commits.
- **The exact before image:** it reports `already_restored` and writes nothing.

Anything else blocks with `restore_state_drift`. This protects changes the receipt does not cover.
It cannot tell whether someone else set the same value on a listed column.

After a restore, a new repair needs a new receipt directory. That is a deliberate operator step.

## After `outcome_unknown`

1. Do not retry the repair.
2. Run `preflight` and read `before_image`, which is the current state.
3. Decide with the owner: keep the state, or restore while the state is exact.
4. Treat any existing scenario as stale.

## Still open

- **Native proof.** Evidence is a fake SDK plus signature checks against the installed jar. These
  are unproven on a real repository: `setScdType(null)`, flush and rollback behaviour, the exact
  text `getExpression().getText()` returns (a difference blocks), and session finder results.
- **Scenario.** A new reviewed version is required. The version pin is unchanged.
- **Rerun checks.** Any no-op or snapshot check for this target must allow for the audit rewrite
  described above.
- **Other mapping expressions and API wiring** are separate work.
