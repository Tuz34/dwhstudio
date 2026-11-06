# DIM_PRODUCT audit-role repair runbook

**Status: code only.** No part of this has run against a real ODI repository. This runbook
does not authorize a live repair, and it does not complete the DIM_PRODUCT cutover.

## Why this exists

`INSERT_DATE`, `UPDATE_DATE`, `SOURCE_SYSTEM` and `ETL_BATCH_ID` on the fixed `DIM_PRODUCT`
datastore have no SCD role. The stock IKM fills `I$` by SCD role, and its final insert reads
every target column from `I$`, so these four columns reach the target as NULL. No mapping
option changes that. This package sets the role of these four columns to
`OVERWRITE_ON_CHANGE`, using the public `OdiColumn.setScdType`.

## What changes after a role-only repair

Read this before approving a role-only repair. It comes from reading the stock IKM tasks, not
from a run. The role-only scope does not change expressions.

- **Volatile audit expressions still rewrite matched current rows.** Task `Flag rows for update`
  marks a row for update when any overwrite-role column differs. `CURRENT_TIMESTAMP` and the
  session batch differ on each run, so unchanged current rows are updated and `INSERT_DATE`
  is refreshed. That breaks a full-snapshot no-op. Do not use the role-only scope when that
  no-op is required.
- **Any existing scenario becomes stale.** It was generated from the old roles. This package
  does not delete, generate or run a scenario. A new version needs its own review.

## Stable expression profile

`stable_repair` sets the same four roles to `OVERWRITE_ON_CHANGE` and replaces only those four
mapping expressions. For each audit column the expression is:

`CASE WHEN EXISTS` a current `DWH.DIM_PRODUCT` row with the same `PROD_ID`, `CURRENT_FLAG = '1'`,
and all 17 tracked attributes equal under `DECODE(C.attr, SRC.attr, 0, 1) = 0`, `THEN` the scalar
select of that existing audit value, `ELSE` the original frozen creation expression.

The source alias is the name of the single `ODS_SH_PRODUCTS` component. It is not a parameter.
There is no `ROWNUM`. Two current rows make the scalar query fail. The existing candidate
readiness already returns `blocked_target_multiple_current_rows` before a load when that count
is not zero. This package does not run that load.

An unchanged current row keeps all four audit values, including `INSERT_DATE`. A new key, or a
change to any of the 17 tracked attributes, still gets a new timestamp, the `'SH'` literal and
the session batch. Historical rows are not selected and are not updated by this expression.
`PROD_UNIT_OF_MEASURE` is the only nullable tracked column; `DECODE` treats null as equal to null.
Tracked types are the source types copied by the fixed planner.

The installed stock IKM task `Insert flow into I$ table` loads `(SCD_UPD OR SCD_INS) AND REW AND
!TRG`. Its later target insert reads columns from `I$`. The expression-update task fills the
surrogate from a target expression. Setting the four audit execute hints to `TARGET` would keep
them out of `I$` and could store nulls. This repair does not call `setExecuteOnHint`. A `TARGET`
hint already present on an audit column blocks. `PRODUCT_SK` must stay `TARGET`, which is what
the fixed builder sets.

The other 22 expressions are checked exactly and never written: the natural key and 17 tracked
columns are `alias.column`, `PRODUCT_SK` is `DWH.SEQ_DIM_PRODUCT.NEXTVAL`, `EFFECTIVE_FROM` is
`CURRENT_DATE`, `EFFECTIVE_TO` is `TO_DATE('24000101','YYYYMMDD')`, and `CURRENT_FLAG` is `1`.
A known `Y`/`N` or `9999-12-31` literal blocks with `legacy_expression_gap` and is not rewritten.
Any other drift blocks with `fixed_expression_drift`.

Native ODI generation and the optimizer are still unproven. The SQL above is what the repository
expression is set to. A synthetic SQLite evaluation translates only `DECODE` and the session-batch
token.

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
| `stable_preflight` | `odi87_dim_product_stable_audit_preflight` | `approved=True` |
| `stable_repair` | `odi87_dim_product_stable_audit_repair` | `approved=True`, review, attestation, receipt directory |
| `stable_restore` | `odi87_dim_product_stable_audit_restore` | `approved=True`, review, attestation, receipt directory |

The role-only scopes cannot authorize an expression write. `stable_repair` with
`odi87_dim_product_audit_role_repair` makes no script call.

- Review means `implementation_review_status="approved"`.
- Attestation means `no_active_odi_session_writes_attested=True`. An operator states this. The
  script separately refuses when running, waiting or queued ODI sessions exist, but it cannot
  see someone editing metadata in ODI Studio.
  Session counts are sampled before mutation and again after flush, before commit.
  These samples are not a repository-wide lock; the exclusive operator attestation
  remains necessary throughout the operation.
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
- all 26 target attributes have an expression;
- the 22 non-audit expressions match the frozen literals above;
- the four audit expressions are the whole volatile profile or the whole stable profile;
- no audit execute hint is `TARGET`, and `PRODUCT_SK` is `TARGET`.

Expression text is compared inside the script and never output. Reports carry the profile id, not
the SQL.

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
| `odi87_dim_product_audit_role_receipt.json` | Before the repair call | Role images. Schema 1 is role-only. Schema 2 adds the before and target profile id and hash, and the expression columns that will change. It does not store SQL. |
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
- **Rerun checks.** A role-only repair with the volatile expressions still refreshes audit
  columns on matched rows. The stable profile is the one that keeps those values. Neither claim
  has been proven by a native ODI load.
- **Known legacy flag and end literals** block with `legacy_expression_gap`. They are not repaired
  here.
