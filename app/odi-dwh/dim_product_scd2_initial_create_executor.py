"""Approval-gated real target setup executor for ODI-68 DIM_PRODUCT SCD2."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

OWNER = "DWH"
TARGET_TABLE = "DIM_PRODUCT"
TARGET_SEQUENCE = "SEQ_DIM_PRODUCT"
ODS_BACKUP_TABLE = "ODI68_ODS_PRODUCTS_BKP"
INITIAL_CREATE_APPROVAL_SCOPE = "odi68_dim_product_scd2_initial_create"

_TYPE_SQL = re.compile(r"^[A-Z][A-Z0-9]*(?:\([0-9]+(?:,[0-9]+)?\))?$")
logger = logging.getLogger("dwh_app.dim_product_scd2_initial_create_executor")


class ExecutorVerificationError(RuntimeError):
    """Raised when real target metadata does not match the frozen contract."""


def _target_columns() -> list[dict[str, object]]:
    metadata = [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
    ]
    plan = build_dim_product_scd2_plan(metadata)
    if plan.get("status") != "metadata_ready_for_review":
        raise ExecutorVerificationError("Frozen target metadata is not ready.")
    return [dict(column) for column in plan["target_columns"]]


def _base(
    status: str,
    *,
    approved: object,
    connection_attempted: bool = False,
) -> dict[str, Any]:
    return {
        "mode": "dim_product_scd2_initial_create_executor",
        "status": status,
        "approved": approved is True,
        "requires_user_approval": True,
        "connection_attempted": connection_attempted,
        "will_execute": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_real_table_statement() -> str:
    """Render fixed DIM_PRODUCT DDL from the reviewed 26-column contract."""
    definitions = []
    for column in _target_columns():
        name = str(column["name"])
        data_type = str(column["data_type"])
        if not _TYPE_SQL.fullmatch(data_type):
            raise ValueError("Unreviewed target data type.")
        null_sql = "" if column["nullable"] else " NOT NULL"
        definitions.append(f'  "{name}" {data_type}{null_sql}')
    definitions.append('  CONSTRAINT "PK_DIM_PRODUCT" PRIMARY KEY ("PRODUCT_SK")')
    return (
        f'CREATE TABLE "{OWNER}"."{TARGET_TABLE}" (\n'
        + ",\n".join(definitions)
        + "\n)"
    )


def build_real_sequence_statement() -> str:
    """Render DDL only for the fixed DIM_PRODUCT sequence."""
    return (
        f'CREATE SEQUENCE "{OWNER}"."{TARGET_SEQUENCE}" '
        "START WITH 1 INCREMENT BY 1 NOCYCLE"
    )


def expected_target_metadata_rows() -> list[tuple[Any, ...]]:
    """Return ALL_TAB_COLUMNS-shaped metadata for deterministic tests."""
    rows: list[tuple[Any, ...]] = []
    for column in _target_columns():
        full_type = str(column["data_type"])
        if full_type.startswith(("VARCHAR2(", "CHAR(", "NVARCHAR2(", "NCHAR(")):
            data_type, body = full_type.split("(", 1)
            length = int(body.rstrip(")"))
            precision = scale = None
        elif full_type.startswith("NUMBER("):
            data_type = "NUMBER"
            parts = [int(part) for part in full_type[7:-1].split(",")]
            length = 22
            precision = parts[0]
            scale = parts[1] if len(parts) == 2 else 0
        else:
            data_type = full_type
            length = 11 if data_type == "TIMESTAMP" else 22
            precision = scale = None
        rows.append(
            (
                column["name"],
                data_type,
                length,
                precision,
                scale,
                "Y" if column["nullable"] else "N",
            )
        )
    return rows


def _full_type(
    data_type: object,
    data_length: object,
    precision: object,
    scale: object,
) -> str:
    base = str(data_type or "").upper()
    if base == "TIMESTAMP(6)":
        return "TIMESTAMP"
    if base in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"}:
        return f"{base}({int(data_length)})"
    if base == "NUMBER" and precision is not None:
        if scale not in (None, 0):
            return f"NUMBER({int(precision)},{int(scale)})"
        return f"NUMBER({int(precision)})"
    return base


def _table_exists(cursor: Any) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": OWNER, "table_name": TARGET_TABLE},
    )
    return int(cursor.fetchone()[0]) > 0


def _sequence_metadata(cursor: Any) -> dict[str, object] | None:
    cursor.execute(
        """
        SELECT INCREMENT_BY, CYCLE_FLAG
        FROM ALL_SEQUENCES
        WHERE SEQUENCE_OWNER=:owner AND SEQUENCE_NAME=:sequence_name
        """,
        {"owner": OWNER, "sequence_name": TARGET_SEQUENCE},
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"increment_by": int(row[0]), "cycle": str(row[1]).upper() == "Y"}


def _verify_empty_target(cursor: Any) -> dict[str, int]:
    cursor.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
               DATA_PRECISION, DATA_SCALE, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER=:owner AND TABLE_NAME=:table_name
        ORDER BY COLUMN_ID
        """,
        {"owner": OWNER, "table_name": TARGET_TABLE},
    )
    actual = [
        {
            "name": str(name),
            "data_type": _full_type(data_type, length, precision, scale),
            "nullable": nullable == "Y",
        }
        for name, data_type, length, precision, scale, nullable in cursor.fetchall()
    ]
    expected = [
        {
            "name": column["name"],
            "data_type": column["data_type"],
            "nullable": column["nullable"],
        }
        for column in _target_columns()
    ]
    if actual != expected:
        raise ExecutorVerificationError("Target metadata verification failed.")

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS cc
          ON cc.OWNER=c.OWNER AND cc.CONSTRAINT_NAME=c.CONSTRAINT_NAME
        WHERE c.OWNER=:owner AND c.TABLE_NAME=:table_name
          AND c.CONSTRAINT_TYPE='P' AND cc.COLUMN_NAME='PRODUCT_SK'
        """,
        {"owner": OWNER, "table_name": TARGET_TABLE},
    )
    constraint_count = int(cursor.fetchone()[0])
    if constraint_count != 1:
        raise ExecutorVerificationError("Surrogate-key verification failed.")

    cursor.execute(f'SELECT COUNT(*) FROM "{OWNER}"."{TARGET_TABLE}"')
    row_count = int(cursor.fetchone()[0])
    if row_count != 0:
        raise ExecutorVerificationError("New target is not empty.")
    return {
        "metadata_column_count": len(actual),
        "product_sk_constraint_count": constraint_count,
        "row_count": row_count,
    }


def _verify_sequence(cursor: Any) -> dict[str, object]:
    metadata = _sequence_metadata(cursor)
    if metadata != {"increment_by": 1, "cycle": False}:
        raise ExecutorVerificationError("Sequence verification failed.")
    return {"sequence_increment_by": 1, "sequence_cycle": False}


def _live_preflight_ready(preflight: Mapping[str, Any]) -> bool:
    source = preflight.get("source")
    target = preflight.get("target")
    sequence = preflight.get("sequence")
    return (
        preflight.get("status") == "blocked_target_missing"
        and isinstance(source, Mapping)
        and source.get("exists") is True
        and source.get("column_count") == 24
        and source.get("metadata_status") == "metadata_ready_for_review"
        and isinstance(source.get("row_count"), int)
        and source.get("duplicate_key_group_count") == 0
        and isinstance(target, Mapping)
        and target.get("owner") == OWNER
        and target.get("table") == TARGET_TABLE
        and target.get("exists") is False
        and isinstance(sequence, Mapping)
        and sequence.get("owner") == OWNER
        and sequence.get("sequence") == TARGET_SEQUENCE
        and sequence.get("exists") is False
        and preflight.get("write_attempted") is False
        and preflight.get("raw_rows_included") is False
        and preflight.get("pk_values_included") is False
    )


def _ods_backup_ready(evidence: Mapping[str, Any]) -> bool:
    return (
        evidence.get("owner") == OWNER
        and evidence.get("table") == ODS_BACKUP_TABLE
        and evidence.get("exists") is True
        and evidence.get("column_count") == 24
        and evidence.get("row_count") == 0
    )


def _live_ods_backup_matches(cursor: Any) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": OWNER, "table_name": ODS_BACKUP_TABLE},
    )
    if int(cursor.fetchone()[0]) != 1:
        return False
    cursor.execute(
        "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": OWNER, "table_name": ODS_BACKUP_TABLE},
    )
    if int(cursor.fetchone()[0]) != 24:
        return False
    cursor.execute(f'SELECT COUNT(*) FROM "{OWNER}"."{ODS_BACKUP_TABLE}"')
    return int(cursor.fetchone()[0]) == 0


def _close_resource(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def _executor_gate(
    plan: Mapping[str, Any],
    *,
    live_preflight: Mapping[str, Any],
    ods_backup_evidence: Mapping[str, Any],
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    disposable_proof_status: str,
) -> dict[str, Any] | None:
    if plan.get("status") != "ready_for_initial_create_review":
        return _base("blocked_initial_create_contract", approved=approved)
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review", approved=approved)
    if approved is not True:
        return _base("blocked_until_exact_operator_approval", approved=approved)
    if approval_scope != INITIAL_CREATE_APPROVAL_SCOPE:
        return _base("blocked_invalid_approval_scope", approved=approved)
    if disposable_proof_status != "passed":
        return _base("blocked_pending_disposable_proof", approved=approved)
    if not _live_preflight_ready(live_preflight):
        return _base("blocked_live_preflight", approved=approved)
    if not _ods_backup_ready(ods_backup_evidence):
        return _base("blocked_ods_backup_evidence", approved=approved)
    return None


def execute_dim_product_initial_create(
    plan: Mapping[str, Any],
    *,
    live_preflight: Mapping[str, Any],
    ods_backup_evidence: Mapping[str, Any],
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    disposable_proof_status: str,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Create and verify only the fixed empty DIM target and sequence."""
    blocked = _executor_gate(
        plan,
        live_preflight=live_preflight,
        ods_backup_evidence=ods_backup_evidence,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        disposable_proof_status=disposable_proof_status,
    )
    if blocked is not None:
        return blocked

    connection = None
    cursor = None
    target_created = False
    sequence_created = False
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "rerun_guard_target"
        if _table_exists(cursor):
            return _base(
                "blocked_target_exists",
                approved=approved,
                connection_attempted=True,
            )
        stage = "rerun_guard_sequence"
        if _sequence_metadata(cursor) is not None:
            return _base(
                "blocked_sequence_exists",
                approved=approved,
                connection_attempted=True,
            )
        stage = "verify_ods_backup"
        if not _live_ods_backup_matches(cursor):
            return _base(
                "blocked_ods_backup_changed",
                approved=approved,
                connection_attempted=True,
            )

        stage = "create_sequence"
        cursor.execute(build_real_sequence_statement())
        sequence_created = True
        stage = "create_target"
        cursor.execute(build_real_table_statement())
        target_created = True
        stage = "verify_target"
        target_evidence = _verify_empty_target(cursor)
        stage = "verify_sequence"
        sequence_evidence = _verify_sequence(cursor)
        return {
            **_base(
                "created_pending_scd2_load",
                approved=approved,
                connection_attempted=True,
            ),
            **target_evidence,
            **sequence_evidence,
            "will_execute": True,
            "write_operations_attempted": [
                "create_dim_product_sequence",
                "create_empty_dim_product_target",
            ],
            "target_exists_after_create": True,
            "sequence_exists_after_create": True,
            "cleanup_attempted": False,
            "rerun_guard": "existing_target_or_sequence_blocks_repeat_create",
        }
    except (oracledb.Error, ExecutorVerificationError, TypeError, ValueError) as exc:
        return {
            **_base("failed", approved=approved, connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
            "target_may_exist": target_created,
            "sequence_may_exist": sequence_created,
            "cleanup_attempted": False,
            "cleanup_requires_separate_approval": target_created or sequence_created,
        }
    finally:
        _close_resource(cursor, "cursor")
        _close_resource(connection, "connection")
