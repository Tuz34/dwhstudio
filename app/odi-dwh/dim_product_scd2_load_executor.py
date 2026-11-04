"""Gated expire-then-insert executor for the ODI-68 DIM_PRODUCT SCD2 flow."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from dim_product_scd2_candidate_preflight import (
    DIM_PRODUCT_TRACKED_COLUMNS,
    SEQUENCE_NAME,
    SOURCE_OWNER,
    SOURCE_TABLE,
    TARGET_OWNER,
    TARGET_TABLE,
    candidate_status,
    collect_dim_product_scd2_candidate_evidence,
)

SCD2_LOAD_APPROVAL_SCOPE = "odi68_dim_product_scd2_load_and_rerun_proof"

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
logger = logging.getLogger("dwh_app.dim_product_scd2_load_executor")


class SCD2VerificationError(RuntimeError):
    """Raised when an in-transaction SCD2 invariant fails before commit."""


def _base(
    status: str,
    *,
    approved: object = False,
    connection_attempted: bool = False,
    write_operations_attempted: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "mode": "dim_product_scd2_load_executor",
        "status": status,
        "approved": approved is True,
        "requires_user_approval": True,
        "connection_attempted": connection_attempted,
        "will_execute": False,
        "write_operations_attempted": write_operations_attempted or [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "batch_id_value_included": False,
        "secret_printed": False,
    }


def _change_predicate(target_reference: str) -> str:
    return " OR ".join(
        f'DECODE({target_reference}."{column}", s."{column}", 0, 1) = 1'
        for column in DIM_PRODUCT_TRACKED_COLUMNS
    )


def build_expire_statement() -> str:
    """Render the fixed update that expires changed current versions."""
    target_reference = f'"{TARGET_TABLE}"'
    return f'''UPDATE "{TARGET_OWNER}"."{TARGET_TABLE}"
SET "EFFECTIVE_TO" = :effective_at,
    "CURRENT_FLAG" = 'N',
    "UPDATE_DATE" = SYSTIMESTAMP
WHERE "CURRENT_FLAG" = 'Y'
  AND EXISTS (
      SELECT 1
      FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s
      WHERE s."PROD_ID" = {target_reference}."PROD_ID"
        AND ({_change_predicate(target_reference)})
  )'''


def build_insert_statement() -> str:
    """Render the fixed insert for new current row versions."""
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
SELECT "{TARGET_OWNER}"."{SEQUENCE_NAME}".NEXTVAL,
       {source_values},
       :effective_at,
       DATE '9999-12-31',
       'Y',
       SYSTIMESTAMP,
       NULL,
       'SH',
       :batch_id
FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s
WHERE NOT EXISTS (
    SELECT 1
    FROM "{TARGET_OWNER}"."{TARGET_TABLE}" t
    WHERE t."PROD_ID" = s."PROD_ID"
      AND t."CURRENT_FLAG" = 'Y'
)'''


def _report_evidence(report: Mapping[str, Any]) -> dict[str, int] | None:
    evidence: dict[str, int] = {}
    for key in _EVIDENCE_KEYS:
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        evidence[key] = value
    return evidence


def _candidate_report_ready(report: Mapping[str, Any]) -> bool:
    evidence = _report_evidence(report)
    return (
        report.get("status") == "ready_for_scd2_load_review"
        and report.get("will_execute") is False
        and report.get("write_attempted") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
        and evidence is not None
        and evidence["candidate_count"]
        == evidence["insert_without_current_count"] + evidence["changed_current_count"]
        and candidate_status(evidence) == "ready_for_scd2_load_review"
    )


def _write_gate(
    candidate_preflight: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    batch_id: object,
) -> dict[str, Any] | None:
    if not _candidate_report_ready(candidate_preflight):
        return _base("blocked_candidate_preflight", approved=approved)
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review", approved=approved)
    if approved is not True:
        return _base("blocked_until_exact_operator_approval", approved=approved)
    if approval_scope != SCD2_LOAD_APPROVAL_SCOPE:
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
        logger.error("SCD2 rollback failed: %s", type(exc).__name__)
        return False


def _run_expire_insert_pass(
    cursor: Any,
    *,
    effective_at: Any,
    batch_id: int,
    operations: list[str],
    rerun: bool,
) -> tuple[int, int]:
    operations.append(
        "rerun_expire_check" if rerun else "expire_changed_current_versions"
    )
    cursor.execute(build_expire_statement(), {"effective_at": effective_at})
    expired = int(cursor.rowcount)
    operations.append(
        "rerun_insert_check" if rerun else "insert_new_current_versions"
    )
    cursor.execute(
        build_insert_statement(),
        {"effective_at": effective_at, "batch_id": batch_id},
    )
    inserted = int(cursor.rowcount)
    return expired, inserted


def _verify_first_pass(
    before: Mapping[str, int],
    after: Mapping[str, int],
    *,
    expired: int,
    inserted: int,
) -> None:
    if expired != before["changed_current_count"]:
        raise SCD2VerificationError("Expired count does not match the reviewed candidate count.")
    if inserted != before["candidate_count"]:
        raise SCD2VerificationError("Inserted count does not match the reviewed candidate count.")
    if after["target_count"] != before["target_count"] + inserted:
        raise SCD2VerificationError("Target history count reconciliation failed.")
    if after["current_target_count"] != after["source_count"]:
        raise SCD2VerificationError("Current target count does not match source count.")
    if after["unchanged_current_count"] != after["source_count"]:
        raise SCD2VerificationError("Post-load unchanged count does not match source count.")
    if candidate_status(after) != "rerun_noop":
        raise SCD2VerificationError("Post-load candidate or DQ state is not a clean no-op.")


def execute_dim_product_scd2_load_and_rerun_proof(
    candidate_preflight: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    batch_id: object,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Expire and insert fixed DIM versions, committing only after a no-op rerun."""
    blocked = _write_gate(
        candidate_preflight,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        batch_id=batch_id,
    )
    if blocked is not None:
        return blocked

    reviewed_evidence = _report_evidence(candidate_preflight)
    if reviewed_evidence is None:
        return _base("blocked_candidate_preflight", approved=approved)

    connection = None
    cursor = None
    stage = "open_connection"
    writes: list[str] = []
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "begin_serializable"
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        stage = "recheck_candidates"
        before = collect_dim_product_scd2_candidate_evidence(cursor)
        if before != reviewed_evidence:
            _rollback(connection)
            return _base(
                "blocked_candidate_state_changed",
                approved=approved,
                connection_attempted=True,
            )

        stage = "capture_effective_time"
        cursor.execute("SELECT SYSDATE FROM DUAL")
        effective_at = cursor.fetchone()[0]

        stage = "expire_and_insert"
        expired, inserted = _run_expire_insert_pass(
            cursor,
            effective_at=effective_at,
            batch_id=int(batch_id),
            operations=writes,
            rerun=False,
        )

        stage = "post_load_dq"
        after_first = collect_dim_product_scd2_candidate_evidence(cursor)
        _verify_first_pass(before, after_first, expired=expired, inserted=inserted)

        stage = "same_transaction_rerun_proof"
        rerun_expired, rerun_inserted = _run_expire_insert_pass(
            cursor,
            effective_at=effective_at,
            batch_id=int(batch_id),
            operations=writes,
            rerun=True,
        )
        after_rerun = collect_dim_product_scd2_candidate_evidence(cursor)
        if rerun_expired != 0 or rerun_inserted != 0 or after_rerun != after_first:
            raise SCD2VerificationError("Same-transaction rerun was not a no-op.")

        stage = "commit"
        connection.commit()
        return {
            **_base(
                "passed",
                approved=approved,
                connection_attempted=True,
                write_operations_attempted=writes,
            ),
            "will_execute": True,
            "expired_count": expired,
            "inserted_count": inserted,
            "rerun_expired_count": rerun_expired,
            "rerun_inserted_count": rerun_inserted,
            "source_count": after_rerun["source_count"],
            "target_count": after_rerun["target_count"],
            "current_target_count": after_rerun["current_target_count"],
            "unchanged_count": after_rerun["unchanged_current_count"],
            "remaining_candidate_count": after_rerun["candidate_count"],
            "duplicate_product_sk_group_count": after_rerun["duplicate_product_sk_groups"],
            "duplicate_current_natural_key_group_count": after_rerun[
                "duplicate_current_natural_key_groups"
            ],
            "invalid_current_flag_count": after_rerun["invalid_current_flags"],
            "invalid_effective_date_count": after_rerun["invalid_effective_dates"],
            "source_missing_current_count": after_rerun["source_missing_current_count"],
            "backup_column_count": after_rerun["backup_column_count"],
            "backup_row_count": after_rerun["backup_row_count"],
        }
    except (oracledb.Error, SCD2VerificationError, TypeError, ValueError) as exc:
        rolled_back = _rollback(connection) if connection is not None else False
        return {
            **_base(
                "failed",
                approved=approved,
                connection_attempted=connection is not None,
                write_operations_attempted=writes,
            ),
            "stage": stage,
            "error_type": type(exc).__name__,
            "rolled_back": rolled_back,
        }
    finally:
        _close(cursor, "cursor")
        _close(connection, "connection")
