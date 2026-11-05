# SCD2 application run gate

The generic run route is bound to `GLOBAL`, the context covered by generation's
execution-account proof. Other contexts are refused before any external callback.
With execution grants enabled, every run obtains fresh topology proof for its
server-derived target model and owner, and checks the proof's model/context/schema
before count preflight or remote execution. It never creates or repairs grants on
the run path. Disabling grants keeps the explicit existing skip policy; it does not
claim that an account was proven. The context still remains `GLOBAL`.

Both the run preflight and postflight API now use the strict stock collector:
1/0 flags, 2400 open-end date, unique current natural keys, non-null keys and valid
date ranges. Legacy Y/N or 9999 profiles cannot pass this route. Each count
transaction is read-only and all resources are released by the reviewed collector.
The standalone legacy collector remains available only for explicit legacy tools.

A native DONE result alone is not a green application result. The after-run stock
checks and source/current counts must also pass; otherwise HTTP 502 reports
`run_completed_postflight_failed`, `scenario_execution_completed=true`,
`postflight_checks_passed=false` and manual review. This does not claim to roll back
an already completed external scenario. Unknown or failed remote outcomes remain
unknown/failed, are not retried, and still include the sanitized count postflight.

`postflight_checks_passed` describes only these structural/count checks. It is not
business-row parity, history interval-overlap proof, populated-audit correctness,
full snapshot idempotence or zero physical DML. The separate ODI81 acceptance
harness must prove those additional properties on isolated fixtures. Run preflight
does not require source/current counts to match before an update/insert load.

Offline tests execute the actual aggregate SQL on SQLite-backed synthetic
connections at the service boundary, plus existing mocked HTTP route and remote
contract tests. This is not native Oracle or live ODI acceptance evidence.
