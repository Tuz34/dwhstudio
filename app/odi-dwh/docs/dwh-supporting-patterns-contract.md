# Phase 5 - Supporting Patterns Static Contract

This is a metadata-only decision record. It does not authorize live work.

## Candidate policies

- Late-arriving facts: park as pending until the applicable dimension window resolves.
- Early-arriving dimensions: create an inferred member only in a future reviewed slice, then refresh its attributes.
- Periodic snapshots: monthly append-only snapshot with one reviewed period per run.
- Aggregate refresh: refresh only the reviewed loaded window.

Every selected pattern needs its own issue, metadata contract, DQ counts/status, rerun guard, backup/rollback plan, and
exact operator approval. No pattern has an executor in this slice.

## Safety boundary

- no raw rows; no PK values; no secrets or connection material.
- no executable statements, connection, schema change, scenario run, or load.
- all four patterns remain design-only.
