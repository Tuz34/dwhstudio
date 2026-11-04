"""Absent-only named-backup creator for the fixed ODI-68 DIM_PRODUCT scenario run.

This is the live ``backup_creator`` callback contract documented in
``dim_product_odi_scd2_scenario_run_executor``. It never issues DROP, TRUNCATE,
DELETE or any statement against the PRODUCTS ODS backup (that table is only
counted). It owns no connection configuration: the caller supplies the
connection factory (the DDL user), and importing this module touches nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import oracledb

from dim_product_scd2_scenario_backup import (
    BACKUP_OWNER,
    BACKUP_TABLE,
    EXPECTED_COLUMN_COUNT,
    EXPECTED_ROW_COUNT,
    ODS_BACKUP_TABLE,
    SOURCE_TABLE,
)

ODS_BACKUP_COLUMN_COUNT = 24
ODS_BACKUP_ROW_COUNT = 0
logger = logging.getLogger("dwh_app.dim_product_scd2_backup_creator")


def verify_retained_dim_product_backup(
    connection_factory: Callable[[], Any],
    *,
    owner: str = BACKUP_OWNER,
    source_table: str = SOURCE_TABLE,
    backup_table: str = BACKUP_TABLE,
    expected_column_count: int = EXPECTED_COLUMN_COUNT,
    expected_row_count: int = EXPECTED_ROW_COUNT,
    require_absent: bool = False,
) -> dict[str, Any]:
    """Export the separate read-only retained verifier without changing the creator."""
    from dim_product_scd2_retained_backup_verifier import verify_retained_dim_product_backup as verify

    return verify(
        connection_factory, owner=owner, source_table=source_table,
        backup_table=backup_table, expected_column_count=expected_column_count,
        expected_row_count=expected_row_count, require_absent=require_absent,
    )


class BackupVerificationError(RuntimeError):
    """Raised when a fixed-object metadata or count invariant fails."""


def _scalar(cursor: Any, sql: str, params: dict[str, object] | None = None) -> int:
    cursor.execute(sql, params or {})
    return int(cursor.fetchone()[0])


def _exists(cursor: Any, table: str) -> bool:
    return _scalar(
        cursor,
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": BACKUP_OWNER, "table_name": table},
    ) > 0


def _column_count(cursor: Any, table: str) -> int:
    return _scalar(
        cursor,
        "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": BACKUP_OWNER, "table_name": table},
    )


def _row_count(cursor: Any, table: str) -> int:
    if table not in {SOURCE_TABLE, BACKUP_TABLE, ODS_BACKUP_TABLE}:
        raise ValueError("Unreviewed DIM_PRODUCT backup object.")
    return _scalar(cursor, f'SELECT COUNT(*) FROM "{BACKUP_OWNER}"."{table}"')


def _close(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def _result(status: str, **extra: Any) -> dict[str, Any]:
    """Non-proof shape: the backup gate treats any status other than the exact proof as failed."""
    return {
        "status": status,
        "side_effects": extra.pop("side_effects", False),
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        **extra,
    }


def create_dim_product_named_backup(
    connection_factory: Callable[[], Any],
    *,
    owner: str = BACKUP_OWNER,
    source_table: str = SOURCE_TABLE,
    backup_table: str = BACKUP_TABLE,
    expected_column_count: int = EXPECTED_COLUMN_COUNT,
    expected_row_count: int = EXPECTED_ROW_COUNT,
    require_absent: bool = True,
) -> dict[str, Any]:
    """Create ``DWH.ODI68_DIM_PRODUCT_BKP`` with CTAS only when it is absent.

    Fails closed (no DDL) when the arguments differ from the fixed object, when
    ``require_absent`` is not True, when the backup already exists, or when the
    source or the ODS backup do not match their expected shape. After the CTAS
    it verifies 26 columns and 72 rows and re-checks the ODS backup. If the
    post-create verification fails the table is left in place (no DROP) and
    ``backup_may_exist`` is reported for manual recovery review.
    """
    fixed = (owner, source_table, backup_table, expected_column_count, expected_row_count)
    if fixed != (
        BACKUP_OWNER, SOURCE_TABLE, BACKUP_TABLE, EXPECTED_COLUMN_COUNT, EXPECTED_ROW_COUNT,
    ):
        return _result("blocked_unreviewed_backup_target")
    if require_absent is not True:
        return _result("blocked_require_absent_required")

    connection = None
    cursor = None
    created = False
    observed: dict[str, int] = {}
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "check_backup_absent"
        if _exists(cursor, BACKUP_TABLE):
            return _result("blocked_backup_exists")

        stage = "verify_source"
        if (
            _column_count(cursor, SOURCE_TABLE) != EXPECTED_COLUMN_COUNT
            or _row_count(cursor, SOURCE_TABLE) != EXPECTED_ROW_COUNT
        ):
            raise BackupVerificationError("Source verification failed.")

        stage = "verify_ods_backup_before"
        if (
            not _exists(cursor, ODS_BACKUP_TABLE)
            or _column_count(cursor, ODS_BACKUP_TABLE) != ODS_BACKUP_COLUMN_COUNT
            or _row_count(cursor, ODS_BACKUP_TABLE) != ODS_BACKUP_ROW_COUNT
        ):
            raise BackupVerificationError("ODS backup precondition failed.")

        stage = "create_named_backup"
        created = True  # set before execute: a failed CTAS may still leave an object
        cursor.execute(
            f'CREATE TABLE "{BACKUP_OWNER}"."{BACKUP_TABLE}" AS '
            f'SELECT * FROM "{BACKUP_OWNER}"."{SOURCE_TABLE}"'
        )

        stage = "verify_named_backup"
        columns = _column_count(cursor, BACKUP_TABLE)
        rows = _row_count(cursor, BACKUP_TABLE)
        observed = {"backup_column_count": columns, "backup_row_count": rows}
        if columns != EXPECTED_COLUMN_COUNT or rows != EXPECTED_ROW_COUNT:
            raise BackupVerificationError("Named backup verification failed.")

        stage = "verify_ods_backup_after"
        ods_columns = _column_count(cursor, ODS_BACKUP_TABLE)
        ods_rows = _row_count(cursor, ODS_BACKUP_TABLE)
        if ods_columns != ODS_BACKUP_COLUMN_COUNT or ods_rows != ODS_BACKUP_ROW_COUNT:
            raise BackupVerificationError("ODS backup changed.")

        return {
            "status": "passed",
            "side_effects": True,
            "backup": {
                "owner": BACKUP_OWNER,
                "table": BACKUP_TABLE,
                "source_table": SOURCE_TABLE,
                "exists": True,
                "created_absent_only": True,
                "column_count": columns,
                "row_count": rows,
            },
            "ods_backup": {
                "owner": BACKUP_OWNER,
                "table": ODS_BACKUP_TABLE,
                "exists": True,
                "column_count": ods_columns,
                "row_count": ods_rows,
                "untouched": True,
            },
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
        }
    except (oracledb.Error, BackupVerificationError, TypeError, ValueError) as exc:
        return _result(
            "failed",
            side_effects=created,
            stage=stage,
            error_type=type(exc).__name__,
            backup_may_exist=created,
            automatic_drop_performed=False,
            **observed,
        )
    finally:
        _close(cursor, "cursor")
        _close(connection, "connection")
