# Repository write outcomes

Repository writes are not safely retryable merely because the client saw an error.
The fixed/generic mapping builders, fixed/generic datastore registrars and public
scenario generator distinguish these reviewed transaction outcomes:

| Outcome | Side effects | Required handling |
| --- | --- | --- |
| `no_transaction` | false | No transaction was obtained |
| `rolled_back` | false | No commit attempted and rollback acknowledged |
| `rollback_failed` | unknown | Manual recovery review |
| `commit_outcome_unknown` | unknown | Manual recovery review even if rollback returned |
| `committed` with incomplete report | true | Inspect committed metadata; do not retry blindly |
| `unverified` transport/output | unknown | Read-only reconciliation before any retry |

The Python boundary accepts exact, duplicate-free failure envelopes with consistent
boolean flags. A legacy `side_effects=false` assertion without transaction evidence
cannot establish a clean rollback. Fixed wrappers preserve the same uncertainty.
Read-only probes retain their separate no-write semantics. The SDK-reference IKM
importer retains its explicit `import_attempted` risk contract; rejected reports are
unverified, not evidence that import did not happen.

The generic mapping builder resolves exactly one eligible exact-name KM, pins all
seven stock options and rechecks target IKM, options and every access point after
the final validation. It does not repair anything after that final gate. Generic
normalizers verify component, key, role and LKM assignment counts against the plan.

Synthetic tests run complete Groovy scripts on isolated fake SDK classes, including
commit-applied-then-exception/Error, rollback failure, final-validation mutation and
report-output failure. SDK compilation and fragment tests are separate evidence.
These checks do not prove native repository transactions or live Oracle behavior.
No live execution, automatic repository cleanup or retry is part of this change.
