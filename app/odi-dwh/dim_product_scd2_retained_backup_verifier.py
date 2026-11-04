"""Read-only retained-backup proof for the three frozen ODI-68 DWH tables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import oracledb

from dim_product_scd2_backup_creator import (
    ODS_BACKUP_COLUMN_COUNT,
    ODS_BACKUP_ROW_COUNT,
    BackupVerificationError,
    _column_count,
    _exists,
    _row_count,
    _scalar,
)
from dim_product_scd2_scenario_backup import (
    BACKUP_OWNER,
    BACKUP_TABLE,
    EXPECTED_COLUMN_COUNT,
    EXPECTED_ROW_COUNT,
    ODS_BACKUP_TABLE,
    SOURCE_TABLE,
    call_sanitized,
)


def _metadata(cursor: Any, table: str) -> list[tuple[Any, ...]]:
    if table not in {SOURCE_TABLE, BACKUP_TABLE}:
        raise ValueError("Unreviewed metadata object.")
    # CTAS may change nullability; all other storage/type semantics must match.
    cursor.execute(
        "SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE, DATA_TYPE_OWNER, DATA_LENGTH, "
        "DATA_PRECISION, DATA_SCALE, CHAR_LENGTH, CHAR_USED "
        "FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name "
        "ORDER BY COLUMN_ID",
        {"owner": BACKUP_OWNER, "table_name": table},
    )
    return [tuple(row) for row in cursor.fetchall()]


def _verify(cursor: Any) -> dict[str, Any]:
    if not all(_exists(cursor, table) for table in (SOURCE_TABLE, BACKUP_TABLE, ODS_BACKUP_TABLE)):
        raise BackupVerificationError("Required table missing.")
    source_metadata = _metadata(cursor, SOURCE_TABLE)
    backup_metadata = _metadata(cursor, BACKUP_TABLE)
    if (
        len(source_metadata) != EXPECTED_COLUMN_COUNT
        or source_metadata != backup_metadata
        or any(len(column) != 9 for column in source_metadata)
        or [column[1] for column in source_metadata] != list(range(1, EXPECTED_COLUMN_COUNT + 1))
        or len({column[0] for column in source_metadata}) != EXPECTED_COLUMN_COUNT
        or "PRODUCT_SK" not in {column[0] for column in source_metadata}
    ):
        raise BackupVerificationError("Column metadata mismatch.")
    if any(_row_count(cursor, table) != EXPECTED_ROW_COUNT for table in (SOURCE_TABLE, BACKUP_TABLE)):
        raise BackupVerificationError("Row count mismatch.")
    if (
        _column_count(cursor, ODS_BACKUP_TABLE) != ODS_BACKUP_COLUMN_COUNT
        or _row_count(cursor, ODS_BACKUP_TABLE) != ODS_BACKUP_ROW_COUNT
    ):
        raise BackupVerificationError("ODS backup mismatch.")

    null_count = _scalar(
        cursor, 'SELECT COUNT(*) FROM "DWH"."ODI68_DIM_PRODUCT_BKP" WHERE "PRODUCT_SK" IS NULL'
    )
    duplicate_count = _scalar(
        cursor,
        'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."ODI68_DIM_PRODUCT_BKP" '
        'GROUP BY "PRODUCT_SK" HAVING COUNT(*) > 1)',
    )
    source_minus_backup = _scalar(
        cursor,
        'SELECT COUNT(*) FROM (SELECT * FROM "DWH"."DIM_PRODUCT" '
        'MINUS SELECT * FROM "DWH"."ODI68_DIM_PRODUCT_BKP")',
    )
    backup_minus_source = _scalar(
        cursor,
        'SELECT COUNT(*) FROM (SELECT * FROM "DWH"."ODI68_DIM_PRODUCT_BKP" '
        'MINUS SELECT * FROM "DWH"."DIM_PRODUCT")',
    )
    # Unique backup SKs plus equal row counts make set equality sufficient here.
    if any(value != 0 for value in (null_count, duplicate_count, source_minus_backup, backup_minus_source)):
        raise BackupVerificationError("Retained backup content mismatch.")
    return {
        "status": "passed",
        "side_effects": False,
        "backup": {
            "owner": BACKUP_OWNER,
            "table": BACKUP_TABLE,
            "source_table": SOURCE_TABLE,
            "exists": True,
            "verified_retained": True,
            "metadata_matches": True,
            "column_count": EXPECTED_COLUMN_COUNT,
            "row_count": EXPECTED_ROW_COUNT,
            "surrogate_key_null_count": null_count,
            "surrogate_key_duplicate_group_count": duplicate_count,
            "source_minus_backup_count": source_minus_backup,
            "backup_minus_source_count": backup_minus_source,
        },
        "ods_backup": {
            "owner": BACKUP_OWNER,
            "table": ODS_BACKUP_TABLE,
            "exists": True,
            "column_count": ODS_BACKUP_COLUMN_COUNT,
            "row_count": ODS_BACKUP_ROW_COUNT,
            "untouched": True,
        },
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _failure(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "side_effects": False,
        "backup_creation_attempted": False,
        "backup_may_exist": True,
        "automatic_drop_performed": False,
        "automatic_restore_performed": False,
        "manual_recovery_review_required": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        **extra,
    }


def _cleanup_action(action: Callable[[], Any]) -> bool:
    action()
    return True


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
    """Verify an existing backup without creating, restoring or changing anything.

    The factory must supply a dedicated connection. The first SQL statement is
    SET TRANSACTION READ ONLY; all later SQL is fixed-object SELECT. The snapshot
    is always ended with rollback, and cursor/connection closure is attempted
    even after errors. Cleanup failure invalidates the proof. No configuration,
    credentials, rows or key values are read into the returned report.
    """
    expected = (BACKUP_OWNER, SOURCE_TABLE, BACKUP_TABLE, EXPECTED_COLUMN_COUNT, EXPECTED_ROW_COUNT)
    actual = (owner, source_table, backup_table, expected_column_count, expected_row_count)
    if any(type(value) is not type(fixed) or value != fixed for value, fixed in zip(actual, expected)):
        return _failure("blocked_unreviewed_backup_target")
    if require_absent is not False:
        return _failure("blocked_retained_verification_required")

    connection = None
    cursor = None
    stage = "open_connection"
    result: dict[str, Any]
    cleanup_failed = False
    try:
        connection = connection_factory()
        stage = "open_cursor"
        cursor = connection.cursor()
        stage = "read_only_transaction"
        cursor.execute("SET TRANSACTION READ ONLY")
        stage = "verify_retained_backup"
        result = _verify(cursor)
    except (oracledb.Error, BackupVerificationError, TypeError, ValueError, IndexError) as exc:
        result = _failure("failed", stage=stage, error_type=type(exc).__name__)
    finally:
        if connection is not None:
            if call_sanitized(_cleanup_action, connection.rollback) is not True:
                cleanup_failed = True
        for resource in (cursor, connection):
            if resource is not None:
                if call_sanitized(_cleanup_action, resource.close) is not True:
                    cleanup_failed = True
    if cleanup_failed:
        return _failure("failed", stage="read_only_cleanup", error_type="ReadOnlyCleanupError")
    return result
