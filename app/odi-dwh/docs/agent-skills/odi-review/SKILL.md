---
name: odi-review
description: Run a targeted, independent, read-only review for the odi-dwh workspace. Use only when the user explicitly asks to review an ODI Linear issue, review-ready slice, security boundary, Groovy change, API gate, or test evidence. Read the exact Linear issue first, inspect only named/current files, and return severity-ordered findings without modifying source files.
---

# ODI Review

Perform an evidence-based review without editing the workspace.

## Workflow

1. Read the exact Linear issue and its latest comments. Treat Linear as the source of truth.
2. Extract the claimed files, behavior, review focus, and test commands from that issue only.
3. Read the compact workspace `CLAUDE.md` only for current rules and baseline. Never read `docs/archive` wholesale.
4. Inspect named files. When a file is missing, use a narrow `rg` search for the claimed symbol or endpoint; do not inventory the whole workspace without cause.
5. Check the relevant boundaries:
   - strict backend approval before any connection, subprocess, or persistent action;
   - rate limits and AppSec route classification;
   - fixed public errors with safe `error_type`, never raw exception text;
   - no secret, connection-string, raw-row, sample-key, or PK-value exposure;
   - metadata/count/status-only evidence;
   - rerun, rollback, and transaction behavior;
   - route path/method/endpoint-name drift;
   - tests that exercise behavior rather than source strings alone.
6. Run focused offline tests when they do not start the app or touch live systems. Re-run full tests/AppSec only when the delivery claims them or the blast radius justifies them.
7. Return findings first, ordered by severity, with precise file and line references. State `No new findings` when appropriate, then list test gaps or residual risk.

## Hard Boundaries

- Do not modify source, tests, docs, Linear state, or comments while reviewing unless the user separately asks to post the verdict.
- Do not inspect `.env`, credentials, secrets, connection strings, raw data, or PK values.
- Do not start/restart the app or execute live DB, ODI, Groovy, model, Docker, WebLogic, or Task Scheduler actions.
- Do not use Git or Gitea.
- Do not treat a passing static source-pattern test as sufficient evidence when behavior can be tested directly.
- Do not infer approval from issue status, prior comments, or broad standing permission.

## Output

Use this order:

1. Findings with severity and file/line references.
2. Open questions or assumptions.
3. Verification performed and commands/results.
4. Concise verdict: `APPROVED`, `NEEDS-REWORK`, or `CANNOT REVIEW FILES`.

Do not repeat issue prose or include raw response bodies.
