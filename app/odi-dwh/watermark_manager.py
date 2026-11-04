"""
ETL Watermark Manager
━━━━━━━━━━━━━━━━━━━━━━━━

Provides watermark-based delta tracking for incremental loads.
Stores each table's last extraction date/value and generates delta queries.

Usage:
  from watermark_manager import WatermarkManager
  wm = WatermarkManager()
  delta_sql = wm.get_delta_query("DWH", "SALES", "MODIFIED_DATE")

Incremental append transaction model (security findings #4 and #8):
  execute_incremental_append() runs everything on ONE target connection:
    INSERT...SELECT -> server-side post-load gates (golden record + DQ, which
    see the still-uncommitted rows on that same connection) -> watermark MERGE
    on that same connection (update_watermark(conn=..., commit=False)) -> ONE
    commit. Any failure rolls back both the append and the watermark, so there
    is no window where rows are committed but the watermark is not (or the
    reverse). Post-load gate results are never accepted from the caller.
  Rerun idempotency: the rerun guard compares the source window with the
  target's committed maximum, so after a successful run a second preflight
  sees an empty window and the executor appends nothing.
  Concurrency: the executor first takes a per-spec lock on the target
  connection (SELECT ... FOR UPDATE on the spec's AUDIT_USER.ETL_WATERMARK row,
  or LOCK TABLE ... EXCLUSIVE when the row does not exist yet), then recomputes
  the target high-water mark. If it moved since preflight, the request is
  answered with blocked_by_rerun_guard and nothing is written. The lock is held
  through the single commit/rollback, so a concurrent second request waits and
  then sees the committed window.
  Executor statuses (all counts/status only, no raw rows):
    ready_for_executor -> executed: appended, gates passed, watermark advanced,
      one commit.
    blocked_until_explicit_approval / blocked_by_rerun_guard: nothing written
      (also returned when the window changed under the lock).
    blocked_no_common_append_columns: nothing written.
    rolled_back_post_load_gate_failed: a server-side gate failed or could not
      be evaluated (oracledb.Error); append rolled back, watermark untouched.
    rolled_back_watermark_update_failed: watermark MERGE failed; append and
      watermark rolled back together, safe_to_retry is true.
  Any other exception rolls back and propagates; the route maps it to the
  redacted HTTP 500.
  Deployment prerequisite: the target connection user needs INSERT/UPDATE on
  AUDIT_USER.ETL_WATERMARK; without it the MERGE fails and everything is
  rolled back (fail closed).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import oracledb
from data_quality import default_post_load_dq_specs, run_post_load_dq_gate
from golden_record_regression import default_specs as default_golden_record_specs
from golden_record_regression import run_live_regressions
from config import (
    ORACLE_USER,
    ORACLE_PASSWORD,
    ORACLE_DSN,
    SOURCE_ORACLE_USER,
    SOURCE_ORACLE_PASSWORD,
    SOURCE_ORACLE_DSN,
    TARGET_ORACLE_USER,
    TARGET_ORACLE_PASSWORD,
    TARGET_ORACLE_DSN,
)

logger = logging.getLogger("watermark_manager")


@dataclass(frozen=True)
class IncrementalStrategySpec:
    """Read-only incremental strategy contract for one source -> ODS target."""

    name: str
    source_table: str
    target_table: str
    key_columns: tuple[str, ...] = ()
    candidate_watermark_columns: tuple[str, ...] = ()
    business_date_columns: tuple[str, ...] = ()
    fact_append: bool = False


def default_incremental_strategy_specs() -> dict[str, IncrementalStrategySpec]:
    """Controlled HR/SH incremental-load specs for ODI-29."""

    return {
        "HR_EMPLOYEES_ODS": IncrementalStrategySpec(
            name="HR_EMPLOYEES_ODS",
            source_table="HR.EMPLOYEES",
            target_table="DWH.ODS_HR_EMPLOYEES",
            key_columns=("EMPLOYEE_ID",),
            candidate_watermark_columns=("LAST_UPDATE_DATE", "UPDATED_AT", "MODIFIED_DATE", "UPDATE_DATE"),
            business_date_columns=("HIRE_DATE",),
            fact_append=False,
        ),
        "SH_SALES_ODS": IncrementalStrategySpec(
            name="SH_SALES_ODS",
            source_table="SH.SALES",
            target_table="DWH.ODS_SH_SALES",
            key_columns=("PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"),
            candidate_watermark_columns=("TIME_ID",),
            business_date_columns=("TIME_ID",),
            fact_append=True,
        ),
    }


def run_configured_incremental_strategy_preflight(spec_names: list[str] | None = None) -> dict[str, Any]:
    """Open configured Oracle connections and run metadata/count-only incremental preflight."""

    specs = _resolve_incremental_strategy_specs(spec_names, default_incremental_strategy_specs())
    source_conn = oracledb.connect(
        user=SOURCE_ORACLE_USER,
        password=SOURCE_ORACLE_PASSWORD,
        dsn=SOURCE_ORACLE_DSN,
    )
    target_conn = oracledb.connect(
        user=TARGET_ORACLE_USER,
        password=TARGET_ORACLE_PASSWORD,
        dsn=TARGET_ORACLE_DSN,
    )
    try:
        return run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def run_incremental_strategy_preflight(
    specs: list[IncrementalStrategySpec],
    *,
    source_conn: Any,
    target_conn: Any,
) -> dict[str, Any]:
    """Run read-only HR/SH incremental strategy checks without loading data."""

    results = [
        _run_one_incremental_strategy_preflight(spec, source_conn=source_conn, target_conn=target_conn)
        for spec in specs
    ]
    all_runnable = bool(results) and all(result["can_run_incremental"] for result in results)
    return {
        "tool": "odi.incremental_strategy_preflight",
        "status": "passed" if all_runnable else "attention_required",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "dry_run_read_only",
        "will_run_incremental": False,
        "secret_printed": False,
        "raw_rows_included": False,
        "results": results,
    }


def _resolve_incremental_strategy_specs(
    names: list[str] | None,
    specs_by_name: dict[str, IncrementalStrategySpec],
) -> list[IncrementalStrategySpec]:
    if not names:
        return list(specs_by_name.values())
    selected = []
    for name in names:
        key = str(name).strip().upper()
        if key not in specs_by_name:
            raise KeyError(f"Unknown incremental strategy spec: {name}")
        selected.append(specs_by_name[key])
    return selected


def _run_one_incremental_strategy_preflight(
    spec: IncrementalStrategySpec,
    *,
    source_conn: Any,
    target_conn: Any,
) -> dict[str, Any]:
    source_owner, source_table = _split_qualified_name(spec.source_table)
    target_owner, target_table = _split_qualified_name(spec.target_table)
    source_columns = _table_columns(source_conn, source_owner, source_table)
    target_columns = _table_columns(target_conn, target_owner, target_table)
    source_row_count = _row_count(source_conn, source_owner, source_table)
    target_row_count = _row_count(target_conn, target_owner, target_table)
    source_pk = _primary_key_columns(source_conn, source_owner, source_table)

    candidates = _normalized_columns(spec.candidate_watermark_columns)
    business_dates = _normalized_columns(spec.business_date_columns)
    found_candidates = [column for column in candidates if column in source_columns]
    business_dates_found = [column for column in business_dates if column in source_columns]
    watermark_column = found_candidates[0] if found_candidates else None

    blockers: list[str] = []
    recommended_next: list[str] = []
    source_max = None
    target_max = None
    estimated_new_rows = None
    target_action = "no_incremental_action"
    rerun_guard = _not_applicable_rerun_guard()
    watermark_advance = _no_watermark_advance(None)

    if spec.fact_append and watermark_column:
        strategy = "incremental_append_with_watermark"
        can_run_incremental = True
        source_max = _max_value(source_conn, source_owner, source_table, watermark_column)
        target_max = _max_value(target_conn, target_owner, target_table, watermark_column)
        estimated_new_rows = _count_after_watermark(
            source_conn,
            source_owner,
            source_table,
            watermark_column,
            target_max,
        )
        rerun_guard = _append_rerun_guard(
            watermark_column=watermark_column,
            source_max=source_max,
            target_max=target_max,
            estimated_new_rows=estimated_new_rows,
        )
        target_action = (
            "append_new_window"
            if rerun_guard["can_append_without_duplicate"]
            else "no_append_duplicate_or_empty_window"
        )
        watermark_advance = _watermark_advance_contract(source_max)
        recommended_next.append("Confirm fact grain and late-arriving data policy before any live incremental load.")
    elif spec.key_columns and watermark_column:
        strategy = "incremental_merge"
        can_run_incremental = True
        source_max = _max_value(source_conn, source_owner, source_table, watermark_column)
        target_max = _max_value(target_conn, target_owner, target_table, watermark_column)
        estimated_new_rows = _count_after_watermark(
            source_conn,
            source_owner,
            source_table,
            watermark_column,
            target_max,
        )
        target_action = "merge_delta_window"
        watermark_advance = _watermark_advance_contract(source_max)
        recommended_next.append("Select merge/upsert KM and validate update semantics before live incremental load.")
    elif spec.key_columns:
        strategy = "full_load_then_merge_by_pk"
        can_run_incremental = False
        blockers.append("no_reliable_update_watermark")
        recommended_next.extend([
            "Choose a CDC design: source update timestamp, materialized view log, GoldenGate, or full-refresh policy.",
            "Do not use business event dates such as HIRE_DATE as an update watermark.",
        ])
    else:
        strategy = "full_load_snapshot"
        can_run_incremental = False
        blockers.append("no_key_or_watermark")
        recommended_next.append("Define source grain and a reliable watermark before live incremental load.")

    predicate_template = f"{_qident(watermark_column)} > :last_watermark" if watermark_column else None
    dry_run_delta = {
        "predicate_template": predicate_template,
        "estimated_new_rows": estimated_new_rows,
        "window_start_exclusive": _safe_value(target_max),
        "window_end_inclusive": _safe_value(source_max),
    }
    execution_plan = _incremental_execution_plan(
        spec=spec,
        target_action=target_action,
        watermark_column=watermark_column,
        candidate_value=source_max,
        dry_run_delta=dry_run_delta,
        rerun_guard=rerun_guard,
    )
    append_columns = _common_append_columns(source_columns, target_columns)

    return {
        "spec": spec.name,
        "status": "passed" if can_run_incremental else "blocked",
        "source_table": spec.source_table,
        "target_table": spec.target_table,
        "strategy": strategy,
        "can_run_incremental": can_run_incremental,
        "will_run_incremental": False,
        "target_action": target_action,
        "watermark_column": watermark_column,
        "blockers": blockers,
        "business_date_columns_not_used_as_watermark": [
            column for column in business_dates_found if column not in found_candidates
        ],
        "source": {
            "row_count": source_row_count,
            "column_count": len(source_columns),
            "primary_key_columns": source_pk,
            "candidate_watermark_columns_found": found_candidates,
            "max_watermark": _safe_value(source_max),
        },
        "target": {
            "row_count": target_row_count,
            "column_count": len(target_columns),
            "max_watermark": _safe_value(target_max),
        },
        "dry_run_delta": dry_run_delta,
        "append_columns": append_columns,
        "rerun_guard": rerun_guard,
        "watermark_advance": watermark_advance,
        "execution_plan": execution_plan,
        "recommended_next": recommended_next,
        "secret_printed": False,
        "raw_rows_included": False,
    }


def _not_applicable_rerun_guard() -> dict[str, Any]:
    return {
        "state": "not_applicable",
        "can_append_without_duplicate": False,
        "watermark_column": None,
        "protected_by": None,
        "reason": "incremental append is not selected",
    }


def _append_rerun_guard(
    *,
    watermark_column: str,
    source_max: Any,
    target_max: Any,
    estimated_new_rows: int | None,
) -> dict[str, Any]:
    can_append = bool(
        source_max is not None
        and target_max is not None
        and source_max > target_max
        and (estimated_new_rows or 0) > 0
    )
    return {
        "state": "new_window_ready" if can_append else "duplicate_or_empty_window_blocked",
        "can_append_without_duplicate": can_append,
        "watermark_column": watermark_column,
        "protected_by": "high_water_window",
        "reason": (
            "source high-water is greater than target high-water"
            if can_append
            else "source high-water is not greater than target high-water or no new rows were estimated"
        ),
    }


def _watermark_advance_contract(candidate_value: Any) -> dict[str, Any]:
    return {
        "will_advance": False,
        "candidate_value": _safe_value(candidate_value),
        "advance_after": ["load_success", "post_load_golden_record_passed", "post_load_dq_passed"],
        "advance_on_failure": False,
    }


def _no_watermark_advance(candidate_value: Any) -> dict[str, Any]:
    return {
        "will_advance": False,
        "candidate_value": _safe_value(candidate_value),
        "advance_after": [],
        "advance_on_failure": False,
    }


def _incremental_execution_plan(
    *,
    spec: IncrementalStrategySpec,
    target_action: str,
    watermark_column: str | None,
    candidate_value: Any,
    dry_run_delta: dict[str, Any],
    rerun_guard: dict[str, Any],
) -> dict[str, Any]:
    can_append = target_action == "append_new_window" and bool(rerun_guard.get("can_append_without_duplicate"))
    append_blocked = target_action == "no_append_duplicate_or_empty_window"
    if can_append:
        status = "awaiting_explicit_approval"
        write_operations = ["append_delta_rows_to_target", "advance_watermark_after_post_load_gates"]
    elif append_blocked:
        status = "blocked_by_rerun_guard"
        write_operations = []
    else:
        status = "not_applicable"
        write_operations = []

    return {
        "mode": "proposal_only",
        "status": status,
        "will_execute": False,
        "requires_user_approval": True,
        "write_operations": write_operations,
        "watermark_store": {
            "owner": "AUDIT_USER",
            "table": "ETL_WATERMARK",
            "key": spec.name,
            "column": watermark_column,
            "candidate_value": _safe_value(candidate_value),
            "advance_on_failure": False,
        },
        "source_window": dry_run_delta,
        "required_gates": [
            "explicit_user_approval",
            "rerun_guard_can_append",
            "load_success",
            "post_load_golden_record_passed",
            "post_load_dq_passed",
        ] if can_append else [],
        "failure_policy": {
            "advance_watermark": False,
            "operator_action": "review_failure_before_retry",
        },
        "raw_rows_included": False,
    }


def build_incremental_execution_request(preflight_result: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    """Evaluate whether a future incremental append request may proceed.

    This approval check is deliberately fail-closed: it never opens a DB
    connection, never appends rows, and never advances the watermark. The live
    executor must call this gate and proceed only from ready_for_executor.
    """

    base = {
        "mode": "approval_gate",
        "approved": bool(approved),
        "will_execute": False,
        "write_operations_attempted": [],
        "watermark_update_attempted": False,
        "requires_user_approval": True,
        "raw_rows_included": False,
        "secret_printed": False,
    }
    if not approved:
        return {
            **base,
            "status": "blocked_until_explicit_approval",
            "reason": "explicit user approval is required before any incremental append or watermark write",
        }

    rerun_guard = preflight_result.get("rerun_guard") or {}
    execution_plan = preflight_result.get("execution_plan") or {}
    if (
        rerun_guard.get("state") != "new_window_ready"
        or rerun_guard.get("can_append_without_duplicate") is not True
        or execution_plan.get("status") != "awaiting_explicit_approval"
    ):
        return {
            **base,
            "status": "blocked_by_rerun_guard",
            "reason": "rerun guard is not in a new_window_ready state",
        }

    return {
        **base,
        "status": "ready_for_executor",
        "reason": "approved request passed rerun guard and is ready for the live executor",
    }


# Controlled mapping from incremental/DQ spec names to golden-record spec keys.
_GOLDEN_SPEC_KEY_BY_INCREMENTAL_SPEC = {
    "HR_EMPLOYEES_ODS": "HR.EMPLOYEES",
    "SH_SALES_ODS": "SH.SALES",
}


# Only database errors raised while evaluating a gate are an expected, handled
# gate failure (fail closed). Everything else (programming errors, bad
# arguments) propagates to the executor rollback-and-raise handler and the
# route returns the redacted HTTP 500.
_GATE_ERRORS = (oracledb.Error,)


class _UnknownGateSpec(Exception):
    """No controlled gate spec exists for the incremental spec (fail closed)."""


def _lock_spec_and_recheck_window(
    target_conn: Any,
    *,
    spec_name: str,
    target_table: str,
    watermark_column: str | None,
    expected_start: Any,
) -> bool:
    """Serialize executions per spec, then verify the window is still new.

    Takes the lock on ``target_conn`` and keeps it until that connection's
    commit/rollback. Returns True when the target high-water mark still equals
    the window start seen at preflight.
    """

    cursor = target_conn.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM AUDIT_USER.ETL_WATERMARK "
            "WHERE TABLE_OWNER = :owner AND TABLE_NAME = :tname FOR UPDATE WAIT 30",
            {"owner": "INCREMENTAL_STRATEGY", "tname": spec_name.upper()},
        )
        if cursor.fetchone() is None:
            # No watermark row yet: serialize on the table instead.
            cursor.execute("LOCK TABLE AUDIT_USER.ETL_WATERMARK IN EXCLUSIVE MODE WAIT 30")
    finally:
        cursor.close()
    owner, table = _split_qualified_name(target_table)
    current = _max_value(target_conn, owner, table, str(watermark_column))
    return _coerce_extract_date(_safe_value(current)) == _coerce_extract_date(expected_start)


def _compute_post_load_gates(
    spec_name: str,
    *,
    source_conn: Any,
    target_conn: Any,
) -> dict[str, bool]:
    """Run both post-load gates server-side on the append connection.

    The gates run on the SAME target connection that performed the INSERT, so
    they observe the uncommitted rows. Only pass/fail booleans leave this
    function; the internal golden report (key differences) is never returned.
    Any error or unknown spec fails closed.
    """

    name = str(spec_name).strip().upper()
    golden_passed = False
    dq_passed = False
    golden_key = _GOLDEN_SPEC_KEY_BY_INCREMENTAL_SPEC.get(name)
    golden_spec = default_golden_record_specs().get(golden_key) if golden_key else None
    dq_spec = default_post_load_dq_specs().get(name)
    try:
        if golden_spec is None:
            raise _UnknownGateSpec
        report = run_live_regressions(
            [golden_spec],
            source_conn=source_conn,
            target_conn=target_conn,
            row_limit=None,
        )
        golden_passed = report.get("status") == "passed"
    except (*_GATE_ERRORS, _UnknownGateSpec) as exc:
        logger.warning("Post-load golden record gate errored: %s", type(exc).__name__)
    try:
        if dq_spec is None:
            raise _UnknownGateSpec
        dq_report = run_post_load_dq_gate([dq_spec], conn=target_conn)
        dq_passed = dq_report.get("status") == "passed"
    except (*_GATE_ERRORS, _UnknownGateSpec) as exc:
        logger.warning("Post-load DQ gate errored: %s", type(exc).__name__)
    return {
        "post_load_golden_record_passed": golden_passed,
        "post_load_dq_passed": dq_passed,
    }


def execute_incremental_append(
    preflight_result: dict[str, Any],
    *,
    approved: bool,
    target_conn: Any,
    source_conn: Any,
    watermark_writer: Any,
) -> dict[str, Any]:
    """Execute a server-side append for an approved, rerun-guarded window.

    Single-transaction semantics on ``target_conn``: INSERT...SELECT, then the
    server-computed post-load gates, then the watermark MERGE, then ONE commit.
    Any gate failure or exception rolls back both the append and the watermark.
    The function never fetches source rows into Python.
    """

    request = build_incremental_execution_request(preflight_result, approved=approved)
    if request["status"] != "ready_for_executor":
        return request

    source_table = preflight_result["source_table"]
    target_table = preflight_result["target_table"]
    watermark_column = preflight_result.get("watermark_column")
    dry_run_delta = preflight_result.get("dry_run_delta") or {}
    last_watermark = _watermark_bind_value(dry_run_delta.get("window_start_exclusive"))
    candidate_watermark = dry_run_delta.get("window_end_inclusive")
    append_columns = [str(column).upper() for column in preflight_result.get("append_columns") or []]
    if not append_columns:
        return {
            **request,
            "status": "blocked_no_common_append_columns",
            "reason": "source and target have no common append columns",
        }

    write_ops = ["append_delta_rows_to_target", "advance_watermark_after_post_load_gates"]
    cursor = target_conn.cursor()
    try:
        if not _lock_spec_and_recheck_window(
            target_conn,
            spec_name=str(preflight_result["spec"]),
            target_table=target_table,
            watermark_column=watermark_column,
            expected_start=last_watermark,
        ):
            target_conn.rollback()
            return {
                **request,
                "status": "blocked_by_rerun_guard",
                "reason": "target high-water mark changed since preflight; window already appended",
                "will_execute": False,
                "rows_appended": 0,
                "write_operations_attempted": [],
                "watermark_update_attempted": False,
            }
        sql = _append_insert_select_sql(
            source_table=source_table,
            target_table=target_table,
            append_columns=append_columns,
            watermark_column=watermark_column,
        )
        cursor.execute(sql, {"last_watermark": last_watermark})
        row_count = int(getattr(cursor, "rowcount", 0) or 0)

        gates = _compute_post_load_gates(
            str(preflight_result["spec"]),
            source_conn=source_conn,
            target_conn=target_conn,
        )
        failed_gates = _failed_post_load_gates(gates)
        if failed_gates:
            target_conn.rollback()
            return {
                **request,
                "status": "rolled_back_post_load_gate_failed",
                "will_execute": False,
                "rows_appended": 0,
                "post_load_gates": gates,
                "failed_gates": failed_gates,
                "write_operations_attempted": ["append_delta_rows_to_target"],
                "watermark_update_attempted": False,
                "raw_rows_included": False,
                "pk_values_included": False,
            }

        try:
            watermark_writer.update_watermark(
                "INCREMENTAL_STRATEGY",
                str(preflight_result["spec"]),
                extract_date=_coerce_extract_date(candidate_watermark),
                extract_value=_safe_value(candidate_watermark),
                row_count=row_count,
                conn=target_conn,
                commit=False,
            )
        except Exception as exc:
            target_conn.rollback()
            return {
                **request,
                "status": "rolled_back_watermark_update_failed",
                "will_execute": False,
                "rows_appended": 0,
                "post_load_gates": gates,
                "write_operations_attempted": write_ops,
                "watermark_update_attempted": True,
                "watermark_update_status": "failed",
                "watermark_error_type": type(exc).__name__,
                "safe_to_retry": True,
                "requires_manual_reconciliation": False,
                "recovery_action": "Append and watermark were rolled back together; retry after fixing the watermark store.",
                "raw_rows_included": False,
                "pk_values_included": False,
            }

        target_conn.commit()
        return {
            **request,
            "status": "executed",
            "will_execute": True,
            "rows_appended": row_count,
            "post_load_gates": gates,
            "write_operations_attempted": write_ops,
            "watermark_update_attempted": True,
            "watermark_advanced_to": _safe_value(candidate_watermark),
            "raw_rows_included": False,
            "pk_values_included": False,
        }
    except Exception:
        target_conn.rollback()
        raise
    finally:
        cursor.close()


def _common_append_columns(source_columns: set[str], target_columns: set[str]) -> list[str]:
    return sorted(source_columns & target_columns)


def _append_insert_select_sql(
    *,
    source_table: str,
    target_table: str,
    append_columns: list[str],
    watermark_column: str,
) -> str:
    source_owner, source_name = _split_qualified_name(source_table)
    target_owner, target_name = _split_qualified_name(target_table)
    columns = ", ".join(_qident(column) for column in append_columns)
    return (
        f"INSERT INTO {_qname(target_owner, target_name)} ({columns}) "
        f"SELECT {columns} FROM {_qname(source_owner, source_name)} "
        f"WHERE {_qident(watermark_column)} > :last_watermark"
    )


def _failed_post_load_gates(gates: dict[str, bool]) -> list[str]:
    required = ["post_load_golden_record_passed", "post_load_dq_passed"]
    return [gate for gate in required if gates.get(gate) is not True]


def _watermark_bind_value(value: Any) -> Any:
    return _coerce_extract_date(value)


def _coerce_extract_date(value: Any) -> Any:
    if isinstance(value, str):
        for candidate in (value, value.replace("Z", "+00:00")):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
    return value


def _table_columns(conn: Any, owner: str, table: str) -> set[str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :owner AND TABLE_NAME = :tname
            ORDER BY COLUMN_ID
            """,
            {"owner": owner, "tname": table},
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}
    finally:
        cursor.close()


def _primary_key_columns(conn: Any, owner: str, table: str) -> list[str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT cols.COLUMN_NAME
            FROM ALL_CONSTRAINTS cons
            JOIN ALL_CONS_COLUMNS cols
              ON cons.OWNER = cols.OWNER
             AND cons.CONSTRAINT_NAME = cols.CONSTRAINT_NAME
            WHERE cons.OWNER = :owner
              AND cons.TABLE_NAME = :tname
              AND cons.CONSTRAINT_TYPE = 'P'
            ORDER BY cols.POSITION
            """,
            {"owner": owner, "tname": table},
        )
        return [str(row[0]).upper() for row in cursor.fetchall()]
    finally:
        cursor.close()


def _row_count(conn: Any, owner: str, table: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {_qname(owner, table)}")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def _max_value(conn: Any, owner: str, table: str, column: str):
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT MAX({_qident(column)}) FROM {_qname(owner, table)}")
        return cursor.fetchone()[0]
    finally:
        cursor.close()


def _count_after_watermark(conn: Any, owner: str, table: str, column: str, watermark_value: Any) -> int | None:
    if watermark_value is None:
        return None
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT COUNT(*) FROM {_qname(owner, table)} WHERE {_qident(column)} > :last_watermark",
            {"last_watermark": watermark_value},
        )
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def _safe_value(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _split_qualified_name(name: str) -> tuple[str, str]:
    parts = str(name).strip().upper().split(".")
    if len(parts) != 2:
        raise ValueError(f"Expected qualified name OWNER.TABLE, got: {name}")
    owner, table = parts
    _validate_identifier(owner)
    _validate_identifier(table)
    return owner, table


def _normalized_columns(columns) -> list[str]:
    normalized = []
    for column in columns or ():
        value = str(column).strip().upper()
        _validate_identifier(value)
        normalized.append(value)
    return normalized


def _qname(owner: str, table: str) -> str:
    return f"{_qident(owner)}.{_qident(table)}"


def _qident(identifier: str) -> str:
    value = str(identifier).strip().upper()
    _validate_identifier(value)
    return f'"{value}"'


def _validate_identifier(identifier: str) -> None:
    value = str(identifier).strip().upper()
    if not value or not value.replace("_", "").replace("$", "").replace("#", "").isalnum():
        raise ValueError(f"Unsafe Oracle identifier: {identifier}")


class WatermarkManager:
    """Manage watermarks for incremental loads."""

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "WatermarkManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ── Read Watermark ────────────────────────────────────────────────────────
    def get_watermark(self, owner: str, table_name: str) -> dict | None:
        """Return current watermark information for the specified table.

        Args:
            owner: Table owner schema name (e.g. "DWH").
            table_name: Table name (e.g. "SALES").

        Returns:
            Watermark dictionary (last_extract_date, last_extract_value, row_count,
            updated_at), or None if no record exists.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT LAST_EXTRACT_DATE, LAST_EXTRACT_VALUE, ROW_COUNT, UPDATED_AT
                FROM AUDIT_USER.ETL_WATERMARK
                WHERE TABLE_OWNER = :owner AND TABLE_NAME = :tname
            """, {"owner": owner.upper(), "tname": table_name.upper()})

            row = cursor.fetchone()
            if row:
                return {
                    "last_extract_date":  row[0],
                    "last_extract_value": row[1],
                    "row_count":          row[2],
                    "updated_at":         row[3],
                }
            return None
        finally:
            cursor.close()

    # ── Update Watermark ──────────────────────────────────────────────────────
    def update_watermark(self, owner: str, table_name: str,
                         extract_date: datetime = None,
                         extract_value: str = None,
                         row_count: int = None,
                         conn: Any = None,
                         commit: bool = True) -> None:
        """Update the watermark record; create one with MERGE if absent.

        Args:
            owner: Table owner schema name.
            table_name: Table name.
            extract_date: Last extraction date; uses SYSDATE if omitted.
            extract_value: Numeric/text watermark value (optional).
            row_count: Number of inserted/updated rows (optional).
            conn: Optional caller-owned connection. When given, the MERGE runs on
                it so the caller can make the watermark part of its own transaction.
            commit: Commit after the MERGE (default). Pass False together with
                ``conn`` to leave commit/rollback to the caller.
        """
        if conn is None:
            conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                MERGE INTO AUDIT_USER.ETL_WATERMARK tgt
                USING (SELECT :owner AS TABLE_OWNER, :tname AS TABLE_NAME FROM DUAL) src
                ON (tgt.TABLE_OWNER = src.TABLE_OWNER AND tgt.TABLE_NAME = src.TABLE_NAME)
                WHEN MATCHED THEN
                    UPDATE SET
                        LAST_EXTRACT_DATE  = :ext_date,
                        LAST_EXTRACT_VALUE = :ext_value,
                        ROW_COUNT          = :rcount,
                        UPDATED_AT         = SYSTIMESTAMP
                WHEN NOT MATCHED THEN
                    INSERT (TABLE_OWNER, TABLE_NAME, LAST_EXTRACT_DATE, LAST_EXTRACT_VALUE, ROW_COUNT)
                    VALUES (:owner2, :tname2, :ext_date2, :ext_value2, :rcount2)
            """, {
                "owner":      owner.upper(),
                "tname":      table_name.upper(),
                "ext_date":   extract_date or datetime.now(),
                "ext_value":  extract_value,
                "rcount":     row_count or 0,
                "owner2":     owner.upper(),
                "tname2":     table_name.upper(),
                "ext_date2":  extract_date or datetime.now(),
                "ext_value2": extract_value,
                "rcount2":    row_count or 0,
            })
            if commit:
                conn.commit()
            logger.info(
                "✅ Watermark updated: %s.%s → %s (%d rows)",
                owner, table_name, extract_value, row_count or 0
            )
        finally:
            cursor.close()

    # ── Generate Delta Query ──────────────────────────────────────────────────
    def get_delta_query(self, owner: str, table_name: str,
                        date_column: str = "MODIFIED_DATE") -> str:
        """Generate a delta query based on the watermark.

        Args:
            owner: Table owner schema name.
            table_name: Table name.
            date_column: Column name to filter by date.

        Returns:
            Delta SELECT with a WHERE filter if a watermark exists; otherwise full-load SELECT.
        """
        wm = self.get_watermark(owner, table_name)
        qualified_name = _qname(owner, table_name)
        watermark_column = _qident(date_column)

        if wm and wm["last_extract_date"]:
            watermark_date = wm["last_extract_date"].strftime("%Y-%m-%d %H:%M:%S")
            sql = (
                f"SELECT * FROM {qualified_name}\n"
                f"WHERE {watermark_column} > TO_TIMESTAMP('{watermark_date}', 'YYYY-MM-DD HH24:MI:SS')\n"
                f"ORDER BY {watermark_column}"
            )
            logger.info(
                "📊 Delta query generated: %s.%s (watermark: %s)",
                owner, table_name, watermark_date
            )
        else:
            sql = (
                f"SELECT * FROM {qualified_name}\n"
                f"ORDER BY {watermark_column}"
            )
            logger.info(
                "📊 Full-load query generated: %s.%s (initial load)",
                owner, table_name
            )

        return sql

    # ── List All Watermarks (for UI) ──────────────────────────────────────────
    def list_all_watermarks(self) -> list[dict]:
        """Return all watermark records."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT TABLE_OWNER, TABLE_NAME, LAST_EXTRACT_DATE,
                       LAST_EXTRACT_VALUE, ROW_COUNT, UPDATED_AT
                FROM AUDIT_USER.ETL_WATERMARK
                ORDER BY TABLE_OWNER, TABLE_NAME
            """)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Could not retrieve watermark list: %s", e)
            return []
        finally:
            cursor.close()

    # ── Reset Watermark ───────────────────────────────────────────────────────
    def reset_watermark(self, owner: str, table_name: str):
        """Delete the specified table's watermark (triggers a full load)."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                DELETE FROM AUDIT_USER.ETL_WATERMARK
                WHERE TABLE_OWNER = :owner AND TABLE_NAME = :tname
            """, {"owner": owner.upper(), "tname": table_name.upper()})
            conn.commit()
            logger.warning("⚠️  Watermark reset: %s.%s", owner, table_name)
        finally:
            cursor.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ETL Watermark Manager")
    sub = parser.add_subparsers(dest="cmd")

    # list command.
    sub.add_parser("list", help="List all watermarks")

    # query command.
    q = sub.add_parser("query", help="Generate delta query")
    q.add_argument("--owner", default="DWH")
    q.add_argument("--table", required=True)
    q.add_argument("--date-col", default="MODIFIED_DATE")

    # reset command.
    r = sub.add_parser("reset", help="Reset watermark")
    r.add_argument("--owner", default="DWH")
    r.add_argument("--table", required=True)

    args = parser.parse_args()
    wm = WatermarkManager()

    try:
        if args.cmd == "list":
            marks = wm.list_all_watermarks()
            if marks:
                print(f"\n{'OWNER':<12} {'TABLE':<30} {'LAST EXTRACT':<22} {'ROWS':>10}")
                print("─" * 78)
                for m in marks:
                    dt = m['LAST_EXTRACT_DATE']
                    dt_str = dt.strftime("%Y-%m-%d %H:%M") if dt else "—"
                    print(f"{m['TABLE_OWNER']:<12} {m['TABLE_NAME']:<30} {dt_str:<22} {m['ROW_COUNT'] or 0:>10}")
            else:
                print("\n📭 No watermark records yet.\n")

        elif args.cmd == "query":
            sql = wm.get_delta_query(args.owner, args.table, args.date_col)
            print(f"\n-- Delta query: {args.owner}.{args.table}\n{sql}\n")

        elif args.cmd == "reset":
            wm.reset_watermark(args.owner, args.table)
            print(f"\n✅ {args.owner}.{args.table} watermark reset.\n")

        else:
            parser.print_help()
    finally:
        wm.close()
