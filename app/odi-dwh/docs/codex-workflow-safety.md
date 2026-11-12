# Codex Workflow Safety Contract

## Source of truth

- Linear owns issue scope, review requests, verdicts, and evidence.
- The compact `CLAUDE.md` contains only current handoff state.
- Historical archives are queried by exact issue or symbol only; they are not loaded wholesale.

## On-demand skills

Tracked templates in `app/odi-dwh/docs/agent-skills/` are the source contract for `odi-review` and `odi-readonly-ui-smoke`. Checkout, CI, and these tests do not install or activate them. Copying a template into a local skill directory is optional and stays outside this repository.

- `app/odi-dwh/docs/agent-skills/odi-review/SKILL.md` is an explicit-invocation, read-only review workflow.
- `app/odi-dwh/docs/agent-skills/odi-readonly-ui-smoke/SKILL.md` checks an already-running UI without persistent actions.
- Both templates set `allow_implicit_invocation: false` in `agents/openai.yaml`. They do not run unless a person explicitly selects an installed copy.

## Command safety

`command_safety.py` classifies attempted commands without executing or echoing them. Its only active mode is
`report_only`: risky commands produce metadata findings but the process exits successfully. Blocking activation is not
implemented and requires a separate reviewed change plus exact user approval.

Normal pytest, Ruff, AppSec, frontend test/lint/build, documentation reads, and metadata searches remain available.
Risk categories cover Git/Gitea, sensitive-file reads, persistent DB actions, live ODI/Groovy execution, model runtime
actions, and persistent Task Scheduler/WebLogic/Docker changes.

## Read-only UI smoke

The UI skill may navigate, inspect visible English text, count console/network failures, test informational modals, and
capture metadata-only screenshots. It must stop if the app is not already running. It cannot start/restart the app,
unlock persistent buttons, submit write forms, call write endpoints, or capture raw rows/PK values.

## Dormant Linear heartbeat

The existing cheap-gate prototype remains in a separate local automation tree (`<automation-root>/heartbeat`). It performs no work unless manually
run, and its registration script requires the explicit `-Arm` switch. No recurring task is registered or enabled by
this slice. Idle checks use no LLM; any future activation requires a manual dry run and exact approval.
