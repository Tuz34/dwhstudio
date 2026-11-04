# Groovy Execution Boundary

## Purpose

`odi_orchestrator.GROOVY_ACTION_REGISTRY` is the authoritative inventory for
every Groovy script callable by the Python application. A script that is not in
the registry cannot launch.

Each entry declares one category:

- `read_only`
- `repository_write`
- `scenario_run`
- `load_plan_run`
- `topology_write`
- `credential_change`

Every category except `read_only` requires strict request-scoped approval at
the common `run_groovy` boundary. Truthy strings or numeric values do not count
as approval.

## Secret Transport

Generic `-Dkey=value` transport rejects secret-like keys before process launch.
The credential-change script is classified but cannot use generic property
transport. A future credential-change workflow must use a separately reviewed
non-command-line mechanism and an exact-step operator approval.

## Output And Failure Contract

- The full Java command is never logged.
- Logs contain only action, category, and parameter-key metadata.
- stdout and stderr are centrally redacted before logging, parsing, or return.
- Nonzero exits, structured error status, and legacy leading `ERROR:`, `HATA:`,
  or `FATAL:` markers fail closed.
- Public failure output is fixed text plus `error_type`; exception bodies and
  stack traces are not returned.
- Allowlisted Groovy sources do not call `printStackTrace()`.

## Live Operation Rule

Code and static tests do not authorize a live ODI action. Before a live
repository write, scenario/load-plan run, topology change, or credential
change, record metadata/status evidence, prepare the applicable backup or
rollback path, and obtain approval for that exact operation. Evidence must not
contain raw rows, PK values, secrets, credentials, or connection strings.
