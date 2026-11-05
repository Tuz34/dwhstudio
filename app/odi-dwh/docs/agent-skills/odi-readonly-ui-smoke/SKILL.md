---
name: odi-readonly-ui-smoke
description: Inspect the already-running odi-dwh local UI with Playwright or the in-app browser using read-only interactions only. Use only when the user explicitly requests an ODI UI smoke check, responsive check, console/network check, modal accessibility check, visible-English check, or verification that persistent actions remain disabled.
---

# ODI Read-only UI Smoke

Inspect UI behavior without starting the app or invoking persistent actions.

## Preconditions

1. Confirm `http://localhost:5000/` is already reachable with a status-only check.
2. If unavailable, stop and report `app_not_running`; do not start or restart it.
3. Do not enter credentials or inspect secret-bearing storage, headers, or response bodies.

## Allowed Checks

- Navigate core Data Ops pages and tabs.
- Record page-load status, console error counts, failed-request status/route metadata, and screenshots.
- Check visible UI text remains English.
- Open informational/read-only modals; verify focus, Escape/close behavior, and non-overlap.
- Check desktop and mobile layout without submitting forms.
- Verify persistent buttons are disabled until their documented approval gates are satisfied.

## Forbidden Interactions

Never click or programmatically invoke actions labeled or equivalent to:

- Apply DB, Apply Change, Sync ODI, Run Scenario, Execute, Load, Append;
- Rollback, Autofix, Reconcile Apply, Persist Alert, Save Mapping;
- credential/topology changes, destructive cleanup, or any confirmation dialog for a write.

Do not call write endpoints directly. Do not change checkbox/input state solely to unlock a persistent button.

## Evidence Boundary

- Capture status, metadata, route names, HTTP status classes, console counts, and screenshots only.
- Do not capture raw table rows, sample keys, PK values, credentials, connection strings, or request/response bodies.
- Redact any unexpected sensitive text before reporting; do not preserve it in screenshots.

## Output

Report checked pages/viewports, status-only failures, console/network counts, accessibility/layout findings, and whether persistent actions remained unavailable. State explicitly that no persistent action was invoked.
