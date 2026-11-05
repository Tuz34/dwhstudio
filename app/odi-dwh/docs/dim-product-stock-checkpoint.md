# Fixed post-stock DIM checkpoint

This is executable preparation code, not a live backup or run proof. The new
fixed name is `DWH.ODI87_DIM_PRODUCT_STOCK_BKP`. Legacy scenario and migration
backups remain untouched. There is no caller-selected owner/table, endpoint,
automatic replacement, drop, restore, grant, migration or scenario execution.

`create_stock_checkpoint` requires literal approval, the exact
`odi87_dim_product_stock_checkpoint_create` scope and approved code review before
opening a connection. The injected factory must return a fresh dedicated DWH
connection with autocommit disabled and no unrelated transaction. Before the
single absent-only CTAS it checks owner, namespace absence, frozen 26-column
target metadata, PRODUCT_SK primary key and actual unique/non-null SKs, the stock
72-current/72-total/0-history state, 72 source rows and the retained 24-column,
empty ODS backup. It never writes to those existing objects.

The complete target projection is copied, not a business-only subset. After DDL,
a new explicit read-only transaction verifies all storage/type/order metadata,
row counts, uniqueness and both full-projection MINUS directions. Backup CTAS
nullability may differ; target nullability must match the frozen contract. A
separate `verify_stock_checkpoint` performs only this retained read-only proof.
Neither path returns rows, PK values, SQL or exception bodies.

Oracle DDL implicitly commits and releases ordinary transaction locks. This code
does not claim to hold a source lock across CTAS or guarantee an atomic future
scenario run. The operator must exclude concurrent loads/DDL during cutover and
repeat the retained proof immediately before the separately approved run. A
visible change after CTAS fails the full-snapshot check and leaves the backup for
manual review, never repairs or deletes it. A failed DDL acknowledgement reports
unknown effects even if rollback succeeds. Acknowledged creation remains a known
side effect if verification/cleanup fails. Rollback ends verification only; it
cannot undo Oracle DDL. Every resource release is attempted and process-control
exceptions propagate after cleanup.

A passed checkpoint proves target/backup equality at one observation, not
source/target business parity, populated-audit correctness, history-overlap
semantics or physical zero DML. Equal NULL projections may be vacuous. Native ODI
ownership, the fixed stock scenario version and its generation/run gates are
separate code packets. Legacy evidence is not silently relabeled stock evidence.

Offline tests use actual SQLite full-set comparisons on synthetic 72-row tables
and Oracle metadata fakes. DDL auto-commit and acknowledgement loss are simulated;
the tests are not native Oracle acceptance. No configured connection is created.
