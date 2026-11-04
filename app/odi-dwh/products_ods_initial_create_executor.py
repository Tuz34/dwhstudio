"""Approval-gated DDL executor for the ODI-68 PRODUCTS ODS initial target."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from products_ods_initial_create import _target_columns

OWNER = "DWH"
TARGET_TABLE = "ODS_SH_PRODUCTS"
PROOF_TABLE = "ODI68_ODS_PRODUCTS_PROOF"
DISPOSABLE_PROOF_APPROVAL_SCOPE = "odi68_products_ods_disposable_create_cleanup"
INITIAL_CREATE_APPROVAL_SCOPE = "odi68_products_ods_initial_target_create"

_ALLOWED_TABLES = {(OWNER, TARGET_TABLE), (OWNER, PROOF_TABLE)}
_CONSTRAINT_NAMES = {
    TARGET_TABLE: "UQ_ODS_SH_PRODUCTS_PROD_ID",
    PROOF_TABLE: "UQ_ODI68_ODS_PRODUCTS_PROOF",
}
_TYPE_SQL = re.compile(r"^[A-Z][A-Z0-9]*(?:\([0-9]+(?:,[0-9]+)?\))?$")
logger = logging.getLogger("dwh_app.products_ods_initial_create_executor")


class ExecutorVerificationError(RuntimeError):
    """Raised when post-DDL metadata does not match the frozen contract."""


def _base(status: str, *, approved: object, connection_attempted: bool = False) -> dict[str, Any]:
    return {
        "mode": "products_ods_initial_create_executor",
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


def _validate_table(owner: str, table: str) -> tuple[str, str]:
    normalized = (str(owner or "").upper(), str(table or "").upper())
    if normalized not in _ALLOWED_TABLES:
        raise ValueError("Unreviewed executor table.")
    return normalized


def build_create_table_statement(owner: str, table: str) -> str:
    """Render DDL only for the two frozen target/proof objects."""
    owner, table = _validate_table(owner, table)
    definitions = []
    for column in _target_columns():
        name = str(column["name"])
        data_type = str(column["data_type"])
        if not _TYPE_SQL.fullmatch(data_type):
            raise ValueError("Unreviewed target data type.")
        null_sql = "" if column["nullable"] else " NOT NULL"
        definitions.append(f'  "{name}" {data_type}{null_sql}')
    definitions.append(
        f'  CONSTRAINT "{_CONSTRAINT_NAMES[table]}" UNIQUE ("PROD_ID")'
    )
    return f'CREATE TABLE "{owner}"."{table}" (\n' + ",\n".join(definitions) + "\n)"


def expected_target_metadata_rows() -> list[tuple[Any, ...]]:
    """Return ALL_TAB_COLUMNS-shaped metadata for deterministic executor tests."""
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
        # Oracle reports an unqualified TIMESTAMP with its default precision.
        return "TIMESTAMP"
    if base in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"}:
        return f"{base}({int(data_length)})"
    if base == "NUMBER" and precision is not None:
        if scale not in (None, 0):
            return f"NUMBER({int(precision)},{int(scale)})"
        return f"NUMBER({int(precision)})"
    return base


def _table_exists(cursor: Any, owner: str, table: str) -> bool:
    owner, table = _validate_table(owner, table)
    cursor.execute(
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": owner, "table_name": table},
    )
    return int(cursor.fetchone()[0]) > 0


def _verify_empty_target(cursor: Any, owner: str, table: str) -> dict[str, int]:
    owner, table = _validate_table(owner, table)
    cursor.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
               DATA_PRECISION, DATA_SCALE, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER=:owner AND TABLE_NAME=:table_name
        ORDER BY COLUMN_ID
        """,
        {"owner": owner, "table_name": table},
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
            "name": item["name"],
            "data_type": item["data_type"],
            "nullable": item["nullable"],
        }
        for item in _target_columns()
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
          AND c.CONSTRAINT_TYPE IN ('P','U') AND cc.COLUMN_NAME='PROD_ID'
        """,
        {"owner": owner, "table_name": table},
    )
    unique_count = int(cursor.fetchone()[0])
    if unique_count != 1:
        raise ExecutorVerificationError("Natural-key constraint verification failed.")

    cursor.execute(f'SELECT COUNT(*) FROM "{owner}"."{table}"')
    row_count = int(cursor.fetchone()[0])
    if row_count != 0:
        raise ExecutorVerificationError("New target is not empty.")
    return {
        "metadata_column_count": len(actual),
        "unique_key_constraint_count": unique_count,
        "row_count": row_count,
    }


def _drop_disposable_proof(cursor: Any) -> None:
    cursor.execute(f'DROP TABLE "{OWNER}"."{PROOF_TABLE}" PURGE')


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
    expected_scope: str,
) -> dict[str, Any] | None:
    if plan.get("status") != "ready_for_initial_create_review":
        return _base("blocked_initial_create_contract", approved=approved)
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review", approved=approved)
    if approved is not True:
        return _base("blocked_until_exact_operator_approval", approved=approved)
    if approval_scope != expected_scope:
        return _base("blocked_invalid_approval_scope", approved=approved)
    return None


def execute_products_ods_disposable_create_cleanup_proof(
    plan: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Create, verify, and remove only the fixed disposable proof object."""
    blocked = _executor_gate(
        plan,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        expected_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
    )
    if blocked is not None:
        return blocked

    connection = None
    cursor = None
    created_by_this_call = False
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "check_disposable_absent"
        if _table_exists(cursor, OWNER, PROOF_TABLE):
            return {
                **_base(
                    "blocked_disposable_table_exists",
                    approved=approved,
                    connection_attempted=True,
                ),
                "proof_table_exists_after_cleanup": True,
            }

        stage = "create_disposable"
        cursor.execute(build_create_table_statement(OWNER, PROOF_TABLE))
        created_by_this_call = True
        stage = "verify_disposable"
        evidence = _verify_empty_target(cursor, OWNER, PROOF_TABLE)
        stage = "remove_disposable"
        _drop_disposable_proof(cursor)
        created_by_this_call = False
        stage = "verify_cleanup"
        exists_after = _table_exists(cursor, OWNER, PROOF_TABLE)
        if exists_after:
            raise ExecutorVerificationError("Disposable cleanup verification failed.")
        return {
            **_base("passed", approved=approved, connection_attempted=True),
            **evidence,
            "will_execute": True,
            "write_operations_attempted": [
                "create_disposable_proof_target",
                "remove_disposable_proof_target",
            ],
            "proof_table_exists_after_cleanup": False,
        }
    except (oracledb.Error, ExecutorVerificationError) as exc:
        cleanup_succeeded = False
        if created_by_this_call and cursor is not None:
            try:
                _drop_disposable_proof(cursor)
                cleanup_succeeded = True
                created_by_this_call = False
            except oracledb.Error as cleanup_exc:
                logger.error("Disposable cleanup failed: %s", type(cleanup_exc).__name__)
        return {
            **_base("failed", approved=approved, connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
            "proof_table_exists_after_cleanup": not cleanup_succeeded,
            "cleanup_attempted": created_by_this_call or cleanup_succeeded,
        }
    finally:
        _close_resource(cursor, "cursor")
        _close_resource(connection, "connection")


def execute_products_ods_initial_target_create(
    plan: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    disposable_cleanup_proof_status: str,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Create the fixed empty target only after every reviewed gate passes."""
    blocked = _executor_gate(
        plan,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        expected_scope=INITIAL_CREATE_APPROVAL_SCOPE,
    )
    if blocked is not None:
        return blocked
    if disposable_cleanup_proof_status != "passed":
        return _base("blocked_pending_disposable_cleanup_proof", approved=approved)

    connection = None
    cursor = None
    target_created = False
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "rerun_guard"
        if _table_exists(cursor, OWNER, TARGET_TABLE):
            return _base(
                "blocked_target_exists",
                approved=approved,
                connection_attempted=True,
            )
        stage = "create_target"
        cursor.execute(build_create_table_statement(OWNER, TARGET_TABLE))
        target_created = True
        stage = "verify_target"
        evidence = _verify_empty_target(cursor, OWNER, TARGET_TABLE)
        return {
            **_base("created_pending_type1_load", approved=approved, connection_attempted=True),
            **evidence,
            "will_execute": True,
            "write_operations_attempted": ["create_empty_products_ods_target"],
            "rerun_guard": "target_exists_blocks_repeat_create",
            "target_may_exist": True,
        }
    except (oracledb.Error, ExecutorVerificationError) as exc:
        return {
            **_base("failed", approved=approved, connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
            "target_may_exist": target_created,
            "cleanup_requires_separate_approval": target_created,
        }
    finally:
        _close_resource(cursor, "cursor")
        _close_resource(connection, "connection")
