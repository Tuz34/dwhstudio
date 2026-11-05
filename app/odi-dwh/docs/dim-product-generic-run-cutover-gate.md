# DIM_PRODUCT generic Run cutover gate (ODI-87)

`execute_mapping_run` in `mapping_run_service.py` refuses `MAP_ODS_PRODUCTS_TO_DIM_PRODUCT`
with HTTP 409 and status `blocked_fixed_mapping_cutover_pending`.

## Why

The owner chose a standard, backed-up migration of the fixed DIM_PRODUCT contract to the
stock SCD knowledge module conventions. That cutover has no live proof yet. Until it does,
`POST /api/mapping/run` must not update, generate or start this mapping.

## What it guarantees

- The name is normalized as the route does it (`strip().upper()`), then compared.
- The refusal comes first: no orchestrator import or construction, health check, option
  update, scenario generation, state write or alert.
- The response is fixed text and echoes no request value.
- Version, context and truncate options do not change the result. There is no request
  field, client readiness flag or environment override that opens it.
- Other mappings follow the unchanged path, starting with the health gate.

## What it does not guarantee

- It covers this one entry only. Low-level runners, the fixed `dim_product_*` executors,
  operator scripts and direct orchestrator calls are not blocked by it.
- Read-only probes are untouched.
- It is not evidence that the migration or cutover happened or worked.

## Removal

This gate is temporary. Remove or replace it only in a separately reviewed change, after
a separately reviewed stock cutover proof. A client flag or `ready` boolean is not proof.
