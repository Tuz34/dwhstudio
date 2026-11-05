# Strict stock SCD2 postflight

`collect_stock_postflight` is an additive read-only collector for the stock
`CURRENT_FLAG`1/0 and open-ended date2400-01-01 contract. It does not perform a
load, prove a native scenario, compare tracked business attributes, or prove
that a rerun executed zero physical DML.

Every count/date query is in one explicit read-only transaction on a dedicated
connection whose autocommit is exactly false. Invalid aggregate shapes or
types, failed rollback or failed close cannot produce success or partial counts.
Driver messages are discarded through the existing sanitized callback boundary.
Process-control exceptions propagate after cleanup.

The collector rejects invalid flags, dates, null natural keys and multiple
current rows per natural key. It returns aggregate-only evidence, never row or
key values. The existing `collect_postflight` retains its legacy behavior for
compatibility and is not a strict-stock acceptance witness.

Application run-gate integration is a separate serialized code step after the
concurrent execution-identity work releases `scd2_service.py`. Until that step
is reviewed and merged, this helper's tests do not establish that the API uses
the stricter collector. Code-first Phase A remains open. No live check was run.
