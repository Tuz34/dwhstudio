"""Retained fixed post-stock checkpoint. Import performs no IO or configuration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dim_product_scd2_scenario_backup import call_sanitized
from dim_product_scd2_stock_migration import EXPECTED_COLUMNS, expected_column_metadata_rows
from scd2_postflight import build_stock_postflight_statements

BACKUP_TABLE = "ODI87_DIM_PRODUCT_STOCK_BKP"
CREATE_SCOPE = "odi87_dim_product_stock_checkpoint_create"
_TABLES = frozenset({"DIM_PRODUCT", BACKUP_TABLE, "ODS_SH_PRODUCTS", "ODI68_ODS_PRODUCTS_BKP"})
_PROJECTION = ",".join(f'"{column["name"]}"' for column in EXPECTED_COLUMNS)


def _need(value: bool) -> None:
    if not value:
        raise ValueError("Checkpoint verification failed")


def _table(table: str) -> str:
    _need(type(table) is str and table in _TABLES)
    return f'"DWH"."{table}"'


def _count(cursor: Any, sql: str, params: dict | None = None) -> int:
    cursor.execute(sql, params or {})
    row = cursor.fetchone()
    _need(isinstance(row, (tuple, list)) and len(row) == 1 and type(row[0]) is int and row[0] >= 0)
    return row[0]


def _exists(cursor: Any, table: str) -> int:
    _table(table)
    return _count(cursor, "SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER='DWH' AND OBJECT_NAME=:table", {"table": table})


def _permanent(cursor: Any, table: str) -> None:
    _need(_exists(cursor, table) == 1)
    _need(_count(cursor, "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER='DWH' AND TABLE_NAME=:table AND TEMPORARY='N'",
                 {"table": table}) == 1)


def _metadata(cursor: Any, table: str) -> list[tuple]:
    _table(table)
    cursor.execute("SELECT COLUMN_ID,COLUMN_NAME,DATA_TYPE,DATA_TYPE_OWNER,DATA_LENGTH,DATA_PRECISION,"
                   "DATA_SCALE,CHAR_USED,CHAR_LENGTH,NULLABLE FROM ALL_TAB_COLUMNS "
                   "WHERE OWNER='DWH' AND TABLE_NAME=:table ORDER BY COLUMN_ID", {"table": table})
    rows = [tuple(row) for row in cursor.fetchall()]
    expected = expected_column_metadata_rows()
    _need(len(rows) == len(expected) == 26)
    for ordinal, (row, column) in enumerate(zip(rows, expected), 1):
        _need(len(row) == 10 and type(row[0]) is int and row[0] == ordinal)
        _need(row[3] is None and row[9] in {"Y", "N"})
        _need((row[1], row[2], row[4], row[5], row[6]) == column[:5])
        if table == "DIM_PRODUCT":
            _need(row[9] == column[5])
        if row[2] in {"VARCHAR2", "CHAR"}:
            _need(row[7] == "B" and row[8] == row[4])
    return rows


def _stock(cursor: Any) -> dict[str, int]:
    for table in ("DIM_PRODUCT", "ODS_SH_PRODUCTS", "ODI68_ODS_PRODUCTS_BKP"):
        _permanent(cursor, table)
    _metadata(cursor, "DIM_PRODUCT")
    _need(_count(cursor, "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER='DWH' AND TABLE_NAME=:table",
                 {"table": "ODI68_ODS_PRODUCTS_BKP"}) == 24)
    _need(_count(cursor, 'SELECT COUNT(*) FROM "DWH"."ODI68_ODS_PRODUCTS_BKP"') == 0)
    _need(_count(cursor, """SELECT COUNT(*) FROM ALL_CONSTRAINTS c WHERE c.OWNER='DWH'
        AND c.TABLE_NAME='DIM_PRODUCT' AND c.CONSTRAINT_TYPE='P' AND c.STATUS='ENABLED'
        AND c.VALIDATED='VALIDATED' AND (SELECT COUNT(*) FROM ALL_CONS_COLUMNS k
        WHERE k.OWNER=c.OWNER AND k.CONSTRAINT_NAME=c.CONSTRAINT_NAME)=1 AND EXISTS
        (SELECT 1 FROM ALL_CONS_COLUMNS k WHERE k.OWNER=c.OWNER AND k.CONSTRAINT_NAME=c.CONSTRAINT_NAME
        AND k.COLUMN_NAME='PRODUCT_SK' AND k.POSITION=1)""") == 1)
    statements = build_stock_postflight_statements("DWH", "DIM_PRODUCT", ["PROD_ID"], source=("DWH", "ODS_SH_PRODUCTS"))
    counts = {name: _count(cursor, sql) for name, sql in statements.items() if name != "max_effective_from"}
    for name, value in counts.items():
        _need(value == (72 if name in {"target_total", "target_current", "source_count"} else 0))
    _need(_count(cursor, 'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" WHERE "PRODUCT_SK" IS NULL') == 0)
    _need(_count(cursor, 'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."DIM_PRODUCT" GROUP BY "PRODUCT_SK" HAVING COUNT(*)>1)') == 0)
    return counts


def _verify(cursor: Any) -> dict[str, int]:
    counts = _stock(cursor)
    _permanent(cursor, BACKUP_TABLE)
    target, backup = _metadata(cursor, "DIM_PRODUCT"), _metadata(cursor, BACKUP_TABLE)
    # CTAS may change NOT NULL constraints. Every storage/type/order field must match.
    _need([row[:9] for row in target] == [row[:9] for row in backup])
    counts["backup_rows"] = _count(cursor, f"SELECT COUNT(*) FROM {_table(BACKUP_TABLE)}")
    _need(counts["backup_rows"] == 72)
    for label, table in (("target", "DIM_PRODUCT"), ("backup", BACKUP_TABLE)):
        counts[f"{label}_null_sk"] = _count(cursor, f'SELECT COUNT(*) FROM {_table(table)} WHERE "PRODUCT_SK" IS NULL')
        counts[f"{label}_duplicate_sk_groups"] = _count(cursor, f'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM {_table(table)} GROUP BY "PRODUCT_SK" HAVING COUNT(*)>1)')
        _need(counts[f"{label}_null_sk"] == counts[f"{label}_duplicate_sk_groups"] == 0)
    for label, left, right in (("target_minus_backup", "DIM_PRODUCT", BACKUP_TABLE),
                               ("backup_minus_target", BACKUP_TABLE, "DIM_PRODUCT")):
        counts[label] = _count(cursor, f"SELECT COUNT(*) FROM (SELECT {_PROJECTION} FROM {_table(left)} MINUS SELECT {_PROJECTION} FROM {_table(right)})")
        _need(counts[label] == 0)
    return counts


def _base(create: bool) -> dict[str, Any]:
    return {
        "mode": "fixed_dim_product_post_stock_checkpoint", "status": "blocked", "stage": "approval",
        "backup": {"owner": "DWH", "table": BACKUP_TABLE}, "counts": {},
        "side_effects": False, "connection_attempted": False, "ddl_attempted": False,
        "outcome_unknown": False,
        "ddl_acknowledged": False, "created_absent_only": False, "full_snapshot_equal": False,
        "backup_may_exist": None if create else True, "manual_review_required": False,
        "automatic_drop_performed": False, "automatic_restore_performed": False,
        "business_parity_proven": False, "physical_noop_proven": False,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
    }


def _action(resource: Any, method: str) -> bool:
    getattr(resource, method)()
    return True


def _cleanup(resources: list[Any]) -> bool:
    connection = resources[0] if resources else None
    cursor = resources[1] if len(resources) > 1 else None
    try:
        rolled = connection is None or call_sanitized(_action, connection, "rollback") is True
    finally:
        try:
            closed_cursor = cursor is None or call_sanitized(_action, cursor, "close") is True
        finally:
            closed_connection = connection is None or call_sanitized(_action, connection, "close") is True
    return rolled and closed_cursor and closed_connection


def _execute(factory: Callable[[], Any], resources: list[Any], report: dict, create: bool) -> bool:
    report.update(connection_attempted=True, stage="open_connection")
    connection = factory()
    resources.append(connection)
    _need(connection.autocommit is False)
    cursor = connection.cursor()
    resources.append(cursor)
    if not create:
        cursor.execute("SET TRANSACTION READ ONLY")
    else:
        report["stage"] = "check_owner_and_absence"
        _need(_count(cursor, "SELECT COUNT(*) FROM DUAL WHERE SYS_CONTEXT('USERENV','SESSION_USER')='DWH'") == 1)
        if _exists(cursor, BACKUP_TABLE) != 0:
            report.update(status="blocked", backup_may_exist=True)
            return False
        report["backup_may_exist"] = False
        report["stage"] = "stock_preflight"
        _stock(cursor)
        report.update(stage="create_checkpoint", ddl_attempted=True, backup_may_exist=True, side_effects=None)
        cursor.execute(f"CREATE TABLE {_table(BACKUP_TABLE)} AS SELECT {_PROJECTION} FROM {_table('DIM_PRODUCT')}")
        report.update(ddl_acknowledged=True, created_absent_only=True, side_effects=True)
        # Oracle DDL committed. The new transaction is only a read-only verification.
        cursor.execute("SET TRANSACTION READ ONLY")
    report["stage"] = "verify_checkpoint"
    counts = _verify(cursor)
    report.update(status="passed", stage="completed", counts=counts, full_snapshot_equal=True, backup_may_exist=True)
    return True


def _run(factory: Callable[[], Any], report: dict, create: bool) -> dict[str, Any]:
    resources: list[Any] = []
    try:
        passed = call_sanitized(_execute, factory, resources, report, create)
    finally:
        clean = _cleanup(resources)
    if passed is not True or not clean:
        report.update(full_snapshot_equal=False, counts={})
        report["outcome_unknown"] = report["ddl_attempted"] and not report["ddl_acknowledged"]
        if report["stage"] != "check_owner_and_absence" or passed is None:
            report["status"] = "failed"
        if not clean:
            report.update(status="failed", stage="cleanup")
        report["manual_review_required"] = report["ddl_attempted"] or not clean
    return report


def create_stock_checkpoint(connection_factory: Callable[[], Any], *, approved: object,
                            approval_scope: object, review_status: object = "pending") -> dict[str, Any]:
    """One fixed absent-only CTAS; factory must supply a fresh dedicated connection."""
    report = _base(True)
    if approved is not True or type(approval_scope) is not str or approval_scope != CREATE_SCOPE or review_status != "approved":
        return report
    return _run(connection_factory, report, True)


def verify_stock_checkpoint(connection_factory: Callable[[], Any]) -> dict[str, Any]:
    """Read-only fixed retained-backup proof, never a creation/recovery claim."""
    return _run(connection_factory, _base(False), False)
