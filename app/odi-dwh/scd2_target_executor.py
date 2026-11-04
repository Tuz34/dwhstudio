"""Absent-only target DDL for generic SCD2 targets (DDL user, no DROP path)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

import scd2_builder

logger = logging.getLogger("dwh_app.scd2_target_executor")

_COLUMNS_SQL = """
SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE
FROM ALL_TAB_COLUMNS
WHERE OWNER = :owner AND TABLE_NAME = :table_name
ORDER BY COLUMN_ID
"""


def _full_type(data_type: object, length: object, precision: object, scale: object) -> str:
    base = str(data_type or "").upper()
    if base.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    if base in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"}:
        return f"{base}({int(length)})"
    if base == "NUMBER" and precision is not None:
        if scale not in (None, 0):
            return f"NUMBER({int(precision)},{int(scale)})"
        return f"NUMBER({int(precision)})"
    return base


def read_catalog_columns(
    cursor: Any, owner: str, table: str
) -> list[dict[str, Any]]:
    """Read column metadata (never rows) from the Oracle catalog."""
    cursor.execute(_COLUMNS_SQL, {"owner": owner, "table_name": table})
    return [
        {
            "name": str(name),
            "data_type": _full_type(data_type, length, precision, scale),
            "nullable": nullable == "Y",
        }
        for name, data_type, length, precision, scale, nullable in cursor.fetchall()
    ]


def fetch_source_columns(
    owner: str, table: str, connection_factory: Callable[[], Any]
) -> list[dict[str, Any]]:
    """Return source column metadata; raises ``oracledb.Error`` on failure."""
    connection = connection_factory()
    try:
        cursor = connection.cursor()
        try:
            return read_catalog_columns(cursor, owner, table)
        finally:
            cursor.close()
    finally:
        connection.close()


def _base(status: str, plan: Mapping[str, Any], *, approved: object) -> dict[str, Any]:
    return {
        "mode": "scd2_target_ddl",
        "status": status,
        "approved": approved is True,
        "target": dict(plan["target"]),
        "sequence": plan["surrogate_key"]["sequence"],
        "connection_attempted": False,
        "write_operations_attempted": [],
        "target_created": False,
        "sequence_created": False,
        "grants_applied": 0,
        "grants_failed": 0,
        "cleanup_attempted": False,
        "drop_supported": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _exists(cursor: Any, sql: str, owner: str, name: str) -> bool:
    cursor.execute(sql, {"owner": owner, "name": name})
    return int(cursor.fetchone()[0]) > 0


def _table_exists(cursor: Any, owner: str, table: str) -> bool:
    return _exists(
        cursor,
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER = :owner AND TABLE_NAME = :name",
        owner,
        table,
    )


def _sequence_exists(cursor: Any, owner: str, sequence: str) -> bool:
    return _exists(
        cursor,
        "SELECT COUNT(*) FROM ALL_SEQUENCES WHERE SEQUENCE_OWNER = :owner AND SEQUENCE_NAME = :name",
        owner,
        sequence,
    )


def _matches_contract(actual: list[dict[str, Any]], plan: Mapping[str, Any]) -> bool:
    expected = [
        {"name": n, "data_type": t, "nullable": nullable}
        for n, t, nullable in scd2_builder.expected_target_metadata(plan)
    ]
    return actual == expected


def _error_status(exc: oracledb.Error) -> str:
    text = str(exc)
    if "ORA-01031" in text or "ORA-01950" in text:
        return "blocked_ddl_privilege"
    if "ORA-00955" in text:
        return "blocked_name_in_use"
    return "failed"


def execute_target_ddl(
    plan: Mapping[str, Any],
    *,
    approved: object,
    connection_factory: Callable[[], Any],
    ddl_user: str = "APP_USER",
) -> dict[str, Any]:
    """Create the SCD2 sequence and table only when both are absent.

    Existing objects are never altered or dropped. Partial progress is reported
    explicitly so an operator can decide on cleanup separately.
    """
    if approved is not True:
        return _base("blocked_until_approval", plan, approved=approved)
    owner = plan["target"]["owner"]
    table = plan["target"]["table"]
    sequence = plan["surrogate_key"]["sequence"]
    result = _base("failed", plan, approved=approved)
    connection = cursor = None
    stage = "open_connection"
    try:
        connection = connection_factory()
        result["connection_attempted"] = True
        cursor = connection.cursor()
        stage = "preflight_table"
        if _table_exists(cursor, owner, table):
            actual = read_catalog_columns(cursor, owner, table)
            result["status"] = (
                "already_exists_matching"
                if _matches_contract(actual, plan)
                else "blocked_target_exists_mismatch"
            )
            result["column_count"] = len(actual)
            return result
        stage = "preflight_sequence"
        sequence_present = _sequence_exists(cursor, owner, sequence)
        result["sequence_already_existed"] = sequence_present

        if not sequence_present:
            stage = "create_sequence"
            result["write_operations_attempted"].append("create_sequence")
            cursor.execute(scd2_builder.render_create_sequence(plan))
            result["sequence_created"] = True
        stage = "create_target"
        result["write_operations_attempted"].append("create_target")
        cursor.execute(scd2_builder.render_create_table(plan))
        result["target_created"] = True

        stage = "verify_target"
        actual = read_catalog_columns(cursor, owner, table)
        if not _matches_contract(actual, plan):
            result["status"] = "partial_failure"
            result["stage"] = stage
            result["cleanup_requires_separate_approval"] = True
            return result
        result["column_count"] = len(actual)

        stage = "grants"
        result["write_operations_attempted"].append("grants")
        for statement in scd2_builder.render_grant_statements(plan, ddl_user):
            try:
                cursor.execute(statement)
                result["grants_applied"] += 1
            except oracledb.DatabaseError as exc:
                result["grants_failed"] += 1
                logger.warning("SCD2 grant failed: %s", type(exc).__name__)
        result["status"] = "created" if result["grants_failed"] == 0 else "created_grants_incomplete"
        return result
    except oracledb.Error as exc:
        created_any = result["target_created"] or result["sequence_created"]
        result["status"] = "partial_failure" if created_any else _error_status(exc)
        result["stage"] = stage
        result["error_type"] = type(exc).__name__
        if created_any:
            result["cleanup_requires_separate_approval"] = True
        return result
    finally:
        _close(cursor)
        _close(connection)


def _close(resource: Any) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("SCD2 DDL resource close failed: %s", type(exc).__name__)
