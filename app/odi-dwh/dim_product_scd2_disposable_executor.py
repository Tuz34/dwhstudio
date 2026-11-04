"""Approval-gated disposable DDL proof for the ODI-68 DIM_PRODUCT flow."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

OWNER = "DWH"
PROOF_TABLE = "ODI68_DIM_PRODUCT_PROOF"
PROOF_SEQUENCE = "ODI68_SEQ_DIM_PRODUCT_PROOF"
DISPOSABLE_PROOF_APPROVAL_SCOPE = "odi68_dim_product_scd2_disposable_create_cleanup"

_TYPE_SQL = re.compile(r"^[A-Z][A-Z0-9]*(?:\([0-9]+(?:,[0-9]+)?\))?$")
logger = logging.getLogger("dwh_app.dim_product_scd2_disposable_executor")


class ExecutorVerificationError(RuntimeError):
    """Raised when disposable metadata does not match the frozen contract."""


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
        "mode": "dim_product_scd2_disposable_proof_executor",
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


def build_disposable_table_statement() -> str:
    """Render the fixed proof-table DDL from the reviewed 26-column contract."""
    definitions = []
    for column in _target_columns():
        name = str(column["name"])
        data_type = str(column["data_type"])
        if not _TYPE_SQL.fullmatch(data_type):
            raise ValueError("Unreviewed target data type.")
        null_sql = "" if column["nullable"] else " NOT NULL"
        definitions.append(f'  "{name}" {data_type}{null_sql}')
    definitions.append(
        '  CONSTRAINT "PK_ODI68_DIM_PRODUCT_PROOF" PRIMARY KEY ("PRODUCT_SK")'
    )
    return (
        f'CREATE TABLE "{OWNER}"."{PROOF_TABLE}" (\n'
        + ",\n".join(definitions)
        + "\n)"
    )


def build_disposable_sequence_statement() -> str:
    """Render DDL only for the fixed proof sequence."""
    return (
        f'CREATE SEQUENCE "{OWNER}"."{PROOF_SEQUENCE}" '
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
        {"owner": OWNER, "table_name": PROOF_TABLE},
    )
    return int(cursor.fetchone()[0]) > 0


def _sequence_metadata(cursor: Any) -> dict[str, object] | None:
    cursor.execute(
        """
        SELECT INCREMENT_BY, CYCLE_FLAG
        FROM ALL_SEQUENCES
        WHERE SEQUENCE_OWNER=:owner AND SEQUENCE_NAME=:sequence_name
        """,
        {"owner": OWNER, "sequence_name": PROOF_SEQUENCE},
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"increment_by": int(row[0]), "cycle": str(row[1]).upper() == "Y"}


def _verify_empty_proof_table(cursor: Any) -> dict[str, int]:
    cursor.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
               DATA_PRECISION, DATA_SCALE, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER=:owner AND TABLE_NAME=:table_name
        ORDER BY COLUMN_ID
        """,
        {"owner": OWNER, "table_name": PROOF_TABLE},
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
        raise ExecutorVerificationError("Proof target metadata verification failed.")

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS cc
          ON cc.OWNER=c.OWNER AND cc.CONSTRAINT_NAME=c.CONSTRAINT_NAME
        WHERE c.OWNER=:owner AND c.TABLE_NAME=:table_name
          AND c.CONSTRAINT_TYPE='P' AND cc.COLUMN_NAME='PRODUCT_SK'
        """,
        {"owner": OWNER, "table_name": PROOF_TABLE},
    )
    constraint_count = int(cursor.fetchone()[0])
    if constraint_count != 1:
        raise ExecutorVerificationError("Proof surrogate-key verification failed.")

    cursor.execute(f'SELECT COUNT(*) FROM "{OWNER}"."{PROOF_TABLE}"')
    row_count = int(cursor.fetchone()[0])
    if row_count != 0:
        raise ExecutorVerificationError("Proof target is not empty.")
    return {
        "metadata_column_count": len(actual),
        "product_sk_constraint_count": constraint_count,
        "row_count": row_count,
    }


def _verify_proof_sequence(cursor: Any) -> dict[str, object]:
    metadata = _sequence_metadata(cursor)
    if metadata != {"increment_by": 1, "cycle": False}:
        raise ExecutorVerificationError("Proof sequence verification failed.")
    return {
        "sequence_increment_by": 1,
        "sequence_cycle": False,
    }


def _drop_proof_table(cursor: Any) -> None:
    cursor.execute(f'DROP TABLE "{OWNER}"."{PROOF_TABLE}" PURGE')


def _drop_proof_sequence(cursor: Any) -> None:
    cursor.execute(f'DROP SEQUENCE "{OWNER}"."{PROOF_SEQUENCE}"')


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
    executor_review_status: str,
    approved: object,
    approval_scope: str,
) -> dict[str, Any] | None:
    if plan.get("status") != "ready_for_initial_create_review":
        return _base("blocked_initial_create_contract", approved=approved)
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review", approved=approved)
    if approved is not True:
        return _base("blocked_until_exact_operator_approval", approved=approved)
    if approval_scope != DISPOSABLE_PROOF_APPROVAL_SCOPE:
        return _base("blocked_invalid_approval_scope", approved=approved)
    return None


def execute_dim_product_disposable_create_cleanup_proof(
    plan: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Create, verify, and remove only the fixed DIM proof objects."""
    blocked = _executor_gate(
        plan,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
    )
    if blocked is not None:
        return blocked

    connection = None
    cursor = None
    table_created = False
    sequence_created = False
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "check_disposable_table_absent"
        if _table_exists(cursor):
            return {
                **_base(
                    "blocked_disposable_table_exists",
                    approved=approved,
                    connection_attempted=True,
                ),
                "proof_table_exists_after_cleanup": True,
            }
        stage = "check_disposable_sequence_absent"
        if _sequence_metadata(cursor) is not None:
            return {
                **_base(
                    "blocked_disposable_sequence_exists",
                    approved=approved,
                    connection_attempted=True,
                ),
                "proof_sequence_exists_after_cleanup": True,
            }

        stage = "create_disposable_sequence"
        cursor.execute(build_disposable_sequence_statement())
        sequence_created = True
        stage = "create_disposable_table"
        cursor.execute(build_disposable_table_statement())
        table_created = True
        stage = "verify_disposable_table"
        table_evidence = _verify_empty_proof_table(cursor)
        stage = "verify_disposable_sequence"
        sequence_evidence = _verify_proof_sequence(cursor)

        stage = "remove_disposable_table"
        _drop_proof_table(cursor)
        table_created = False
        stage = "remove_disposable_sequence"
        _drop_proof_sequence(cursor)
        sequence_created = False
        stage = "verify_cleanup"
        table_exists_after = _table_exists(cursor)
        sequence_exists_after = _sequence_metadata(cursor) is not None
        if table_exists_after or sequence_exists_after:
            raise ExecutorVerificationError("Disposable cleanup verification failed.")
        return {
            **_base("passed", approved=approved, connection_attempted=True),
            **table_evidence,
            **sequence_evidence,
            "will_execute": True,
            "write_operations_attempted": [
                "create_disposable_dim_sequence",
                "create_disposable_dim_target",
                "remove_disposable_dim_target",
                "remove_disposable_dim_sequence",
            ],
            "proof_table_exists_after_cleanup": False,
            "proof_sequence_exists_after_cleanup": False,
        }
    except (oracledb.Error, ExecutorVerificationError, TypeError, ValueError) as exc:
        cleanup_attempted = table_created or sequence_created
        if table_created and cursor is not None:
            try:
                _drop_proof_table(cursor)
                table_created = False
            except oracledb.Error as cleanup_exc:
                logger.error("Disposable table cleanup failed: %s", type(cleanup_exc).__name__)
        if sequence_created and cursor is not None:
            try:
                _drop_proof_sequence(cursor)
                sequence_created = False
            except oracledb.Error as cleanup_exc:
                logger.error("Disposable sequence cleanup failed: %s", type(cleanup_exc).__name__)
        return {
            **_base("failed", approved=approved, connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
            "cleanup_attempted": cleanup_attempted,
            "proof_table_exists_after_cleanup": table_created,
            "proof_sequence_exists_after_cleanup": sequence_created,
        }
    finally:
        _close_resource(cursor, "cursor")
        _close_resource(connection, "connection")
