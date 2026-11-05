"""Rollback-only tracked-attribute change proof for ODI-68 DIM_PRODUCT SCD2."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from dim_product_scd2_candidate_preflight import (
    DIM_PRODUCT_TRACKED_COLUMNS,
    SOURCE_OWNER,
    SOURCE_TABLE,
    TARGET_OWNER,
    TARGET_TABLE,
    candidate_status,
    collect_dim_product_scd2_candidate_evidence,
)
from dim_product_scd2_load_executor import build_expire_statement
from dim_product_scd2_stock_contract import (
    SQL_INSERT_AUDIT,
    SQL_SOURCE_SYSTEM,
    SQL_UPDATE_AUDIT,
    STOCK_CURRENT_FLAG,
    STOCK_CURRENT_MATCH_SQL,
    STOCK_OPEN_ENDED_DATE_SQL,
)

CHANGE_PROOF_APPROVAL_SCOPE = (
    "odi68_dim_product_scd2_attribute_change_rollback_proof"
)

_EVIDENCE_KEYS = (
    "source_count",
    "target_count",
    "source_duplicate_groups",
    "duplicate_product_sk_groups",
    "duplicate_current_natural_key_groups",
    "invalid_current_flags",
    "invalid_effective_dates",
    "current_target_count",
    "insert_without_current_count",
    "changed_current_count",
    "unchanged_current_count",
    "source_missing_current_count",
    "backup_exists",
    "backup_column_count",
    "backup_row_count",
    "candidate_count",
)
logger = logging.getLogger("dwh_app.dim_product_scd2_attribute_change_proof")


class SCD2ChangeProofError(RuntimeError):
    """Raised when a rollback-only proof invariant is not satisfied."""


def _base(
    status: str,
    *,
    approved: object = False,
    connection_attempted: bool = False,
    operations: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "mode": "dim_product_scd2_attribute_change_rollback_proof",
        "status": status,
        "approved": approved is True,
        "requires_user_approval": True,
        "connection_attempted": connection_attempted,
        "will_execute": False,
        "write_operations_attempted": operations or [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "batch_id_value_included": False,
        "secret_printed": False,
    }


def build_source_change_statement() -> str:
    """Render a fixed one-row tracked-attribute simulation without returning its key."""
    return f'''UPDATE "{SOURCE_OWNER}"."{SOURCE_TABLE}"
SET "PROD_STATUS" = CASE
    WHEN "PROD_STATUS" = 'available' THEN 'unavailable'
    ELSE 'available'
END
WHERE "PROD_ID" = (
    SELECT MIN("PROD_ID") FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}"
)'''


def build_rollback_proof_insert_statement() -> str:
    """Render the fixed proof insert without advancing the real Oracle sequence."""
    business_columns = ("PROD_ID", *DIM_PRODUCT_TRACKED_COLUMNS)
    target_columns = (
        "PRODUCT_SK",
        *business_columns,
        "EFFECTIVE_FROM",
        "EFFECTIVE_TO",
        "CURRENT_FLAG",
        "INSERT_DATE",
        "UPDATE_DATE",
        "SOURCE_SYSTEM",
        "ETL_BATCH_ID",
    )
    rendered_target_columns = ", ".join(f'"{column}"' for column in target_columns)
    source_values = ", ".join(f's."{column}"' for column in business_columns)
    return f'''INSERT INTO "{TARGET_OWNER}"."{TARGET_TABLE}" (
    {rendered_target_columns}
)
SELECT (
           SELECT COALESCE(MAX("PRODUCT_SK"), 0) + 1
           FROM "{TARGET_OWNER}"."{TARGET_TABLE}"
       ),
       {source_values},
       :effective_at,
       {STOCK_OPEN_ENDED_DATE_SQL},
       '{STOCK_CURRENT_FLAG}',
       {SQL_INSERT_AUDIT},
       {SQL_UPDATE_AUDIT},
       {SQL_SOURCE_SYSTEM},
       :batch_id
FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s
WHERE NOT EXISTS (
    SELECT 1
    FROM "{TARGET_OWNER}"."{TARGET_TABLE}" t
    WHERE t."PROD_ID" = s."PROD_ID"
      AND t.{STOCK_CURRENT_MATCH_SQL}
)'''


def _report_evidence(report: Mapping[str, Any]) -> dict[str, int] | None:
    evidence: dict[str, int] = {}
    for key in _EVIDENCE_KEYS:
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        evidence[key] = value
    return evidence


def _baseline_ready(report: Mapping[str, Any]) -> bool:
    evidence = _report_evidence(report)
    return (
        report.get("status") == "rerun_noop"
        and report.get("will_execute") is False
        and report.get("write_attempted") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
        and evidence is not None
        and evidence["source_count"] > 0
        and evidence["target_count"] == evidence["source_count"]
        and evidence["current_target_count"] == evidence["source_count"]
        and evidence["unchanged_current_count"] == evidence["source_count"]
        and candidate_status(evidence) == "rerun_noop"
    )


def _write_gate(
    baseline: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    batch_id: object,
) -> dict[str, Any] | None:
    if not _baseline_ready(baseline):
        return _base("blocked_baseline_preflight", approved=approved)
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review", approved=approved)
    if approved is not True:
        return _base("blocked_until_exact_operator_approval", approved=approved)
    if approval_scope != CHANGE_PROOF_APPROVAL_SCOPE:
        return _base("blocked_invalid_approval_scope", approved=approved)
    if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id <= 0:
        return _base("blocked_invalid_batch_id", approved=approved)
    return None


def _close(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def _rollback(connection: Any) -> bool:
    try:
        connection.rollback()
        return True
    except oracledb.Error as exc:
        logger.error("SCD2 change proof rollback failed: %s", type(exc).__name__)
        return False


def _changed_state_ready(before: Mapping[str, int], changed: Mapping[str, int]) -> bool:
    return (
        changed["source_count"] == before["source_count"]
        and changed["target_count"] == before["target_count"]
        and changed["current_target_count"] == before["current_target_count"]
        and changed["insert_without_current_count"] == 0
        and changed["changed_current_count"] == 1
        and changed["unchanged_current_count"] == before["source_count"] - 1
        and changed["candidate_count"] == 1
        and candidate_status(changed) == "ready_for_scd2_load_review"
    )


def _loaded_state_ready(
    before: Mapping[str, int],
    loaded: Mapping[str, int],
) -> bool:
    return (
        loaded["source_count"] == before["source_count"]
        and loaded["target_count"] == before["target_count"] + 1
        and loaded["current_target_count"] == before["source_count"]
        and loaded["unchanged_current_count"] == before["source_count"]
        and loaded["candidate_count"] == 0
        and candidate_status(loaded) == "rerun_noop"
    )


def execute_dim_product_scd2_attribute_change_rollback_proof(
    baseline_preflight: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    batch_id: object,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Prove one historical version transition and always roll the transaction back."""
    blocked = _write_gate(
        baseline_preflight,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        batch_id=batch_id,
    )
    if blocked is not None:
        return blocked

    reviewed = _report_evidence(baseline_preflight)
    if reviewed is None:
        return _base("blocked_baseline_preflight", approved=approved)

    connection = None
    cursor = None
    operations: list[str] = []
    blocked_status: str | None = None
    error: Exception | None = None
    proof: dict[str, int] = {}
    rolled_back = False
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "begin_serializable"
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        stage = "recheck_baseline"
        before = collect_dim_product_scd2_candidate_evidence(cursor)
        if before != reviewed:
            blocked_status = "blocked_baseline_state_changed"
        else:
            stage = "simulate_source_attribute_change"
            operations.append("simulate_one_source_attribute_change")
            cursor.execute(build_source_change_statement())
            simulated = int(cursor.rowcount)
            if simulated != 1:
                raise SCD2ChangeProofError("Source simulation did not affect exactly one row.")

            stage = "verify_changed_candidate"
            changed = collect_dim_product_scd2_candidate_evidence(cursor)
            if not _changed_state_ready(before, changed):
                raise SCD2ChangeProofError("Changed candidate state is not exactly one row.")

            stage = "capture_effective_time"
            cursor.execute("SELECT SYSDATE FROM DUAL")
            effective_at = cursor.fetchone()[0]

            stage = "expire_changed_current_version"
            operations.append("expire_changed_current_version")
            cursor.execute(build_expire_statement(), {"effective_at": effective_at})
            expired = int(cursor.rowcount)

            stage = "insert_new_current_version"
            operations.append("insert_new_current_version")
            cursor.execute(
                build_rollback_proof_insert_statement(),
                {"effective_at": effective_at, "batch_id": int(batch_id)},
            )
            inserted = int(cursor.rowcount)
            loaded = collect_dim_product_scd2_candidate_evidence(cursor)
            if expired != 1 or inserted != 1 or not _loaded_state_ready(before, loaded):
                raise SCD2ChangeProofError("Version transition did not produce a clean 1/1 result.")

            stage = "same_transaction_rerun"
            operations.append("rerun_expire_check")
            cursor.execute(build_expire_statement(), {"effective_at": effective_at})
            rerun_expired = int(cursor.rowcount)
            operations.append("rerun_insert_check")
            cursor.execute(
                build_rollback_proof_insert_statement(),
                {"effective_at": effective_at, "batch_id": int(batch_id)},
            )
            rerun_inserted = int(cursor.rowcount)
            after_rerun = collect_dim_product_scd2_candidate_evidence(cursor)
            if rerun_expired != 0 or rerun_inserted != 0 or after_rerun != loaded:
                raise SCD2ChangeProofError("Same-transaction rerun was not a no-op.")

            proof = {
                "simulated_source_change_count": simulated,
                "changed_candidate_count": changed["changed_current_count"],
                "expired_count": expired,
                "inserted_count": inserted,
                "rerun_expired_count": rerun_expired,
                "rerun_inserted_count": rerun_inserted,
                "proof_target_count": loaded["target_count"],
                "proof_current_target_count": loaded["current_target_count"],
                "proof_remaining_candidate_count": loaded["candidate_count"],
            }
    except (oracledb.Error, SCD2ChangeProofError, TypeError, ValueError) as exc:
        error = exc
    finally:
        if connection is not None:
            rolled_back = _rollback(connection)
        _close(cursor, "cursor")
        _close(connection, "connection")

    if connection is not None and not rolled_back:
        return {
            **_base(
                "failed_rollback",
                approved=approved,
                connection_attempted=True,
                operations=operations,
            ),
            "stage": stage,
            "rolled_back": False,
            "persistent_state_preserved": False,
        }
    if blocked_status is not None:
        return {
            **_base(
                blocked_status,
                approved=approved,
                connection_attempted=True,
                operations=operations,
            ),
            "rolled_back": rolled_back,
            "persistent_state_preserved": rolled_back,
        }
    if error is not None:
        return {
            **_base(
                "failed_rolled_back" if rolled_back else "failed",
                approved=approved,
                connection_attempted=connection is not None,
                operations=operations,
            ),
            "stage": stage,
            "error_type": type(error).__name__,
            "rolled_back": rolled_back,
            "persistent_state_preserved": rolled_back,
        }
    return {
        **_base(
            "passed_rolled_back",
            approved=approved,
            connection_attempted=True,
            operations=operations,
        ),
        "will_execute": True,
        **proof,
        "rolled_back": True,
        "persistent_state_preserved": True,
    }
