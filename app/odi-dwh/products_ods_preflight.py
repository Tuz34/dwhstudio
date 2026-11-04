"""Read-only live preflight for the ODI-68 PRODUCTS ODS Type 1 flow."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import oracledb

from dwh_load_pattern_manager import (
    build_products_ods_scd1_plan,
    build_products_ods_target_scd1_plan,
)

_SOURCE = ("SH", "PRODUCTS")
_TARGET = ("DWH", "ODS_SH_PRODUCTS")
_ALLOWED_TABLES = {_SOURCE, _TARGET}
_ARTIFACT_ID = re.compile(r"^ODI68_[A-Z0-9_]{8,56}$")
logger = logging.getLogger("dwh_app.products_ods_preflight")


def _full_oracle_type(
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


def _table_snapshot(cursor: Any, owner: str, table: str) -> dict[str, Any]:
    if (owner, table) not in _ALLOWED_TABLES:
        raise ValueError("Unreviewed preflight table.")

    cursor.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
               DATA_PRECISION, DATA_SCALE, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER = :owner AND TABLE_NAME = :table_name
        ORDER BY COLUMN_ID
        """,
        {"owner": owner, "table_name": table},
    )
    metadata = [
        {
            "name": str(name),
            "data_type": _full_oracle_type(data_type, data_length, precision, scale),
            "nullable": nullable == "Y",
        }
        for name, data_type, data_length, precision, scale, nullable in cursor.fetchall()
    ]
    if not metadata:
        return {
            "owner": owner,
            "table": table,
            "exists": False,
            "column_count": 0,
            "metadata": [],
            "row_count": None,
            "duplicate_key_group_count": None,
        }

    cursor.execute(f'SELECT COUNT(*) FROM "{owner}"."{table}"')
    row_count = int(cursor.fetchone()[0])
    cursor.execute(
        f'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "{owner}"."{table}" '
        'GROUP BY "PROD_ID" HAVING COUNT(*) > 1)'
    )
    duplicate_count = int(cursor.fetchone()[0])
    return {
        "owner": owner,
        "table": table,
        "exists": True,
        "column_count": len(metadata),
        "metadata": metadata,
        "row_count": row_count,
        "duplicate_key_group_count": duplicate_count,
    }


def _read_snapshot(connection: Any, owner: str, table: str) -> dict[str, Any]:
    cursor = connection.cursor()
    try:
        cursor.execute("SET TRANSACTION READ ONLY")
        return _table_snapshot(cursor, owner, table)
    finally:
        cursor.close()


def _rollback_read_only(connection: Any) -> None:
    try:
        connection.rollback()
    except oracledb.Error as exc:
        logger.warning("Read-only rollback failed: %s", type(exc).__name__)


def run_products_ods_read_only_preflight(
    source_connection: Any,
    target_connection: Any,
) -> dict[str, Any]:
    """Collect count/status/metadata evidence without exposing row or key values."""
    try:
        source = _read_snapshot(source_connection, *_SOURCE)
        target = _read_snapshot(target_connection, *_TARGET)
    finally:
        _rollback_read_only(source_connection)
        _rollback_read_only(target_connection)

    source_plan = build_products_ods_scd1_plan(source["metadata"])
    source["metadata_status"] = source_plan["status"]
    source["metadata_drift"] = source_plan["metadata_drift"]

    if target["exists"]:
        target_plan = build_products_ods_target_scd1_plan(target["metadata"])
        target["metadata_status"] = target_plan["status"]
        target["metadata_drift"] = target_plan["metadata_drift"]
    else:
        target["metadata_status"] = "target_missing"
        target["metadata_drift"] = {
            "missing_columns": [],
            "unexpected_columns": [],
            "type_mismatches": [],
        }

    if source_plan["status"] != "metadata_ready_for_review":
        status = "blocked_source_metadata_drift"
    elif source["duplicate_key_group_count"] != 0:
        status = "blocked_source_duplicate_keys"
    elif not target["exists"]:
        status = "blocked_target_missing"
    elif target["metadata_status"] != "metadata_ready_for_review":
        status = "blocked_target_metadata_drift"
    elif target["duplicate_key_group_count"] != 0:
        status = "blocked_target_duplicate_keys"
    else:
        status = "ready_for_named_backup"

    if target["exists"]:
        backup_restore_readiness = {
            "status": "named_backup_required",
            "previous_state": "target_exists",
            "live_backup_created": False,
            "live_restore_tested": False,
        }
    else:
        backup_restore_readiness = {
            "status": "absent_target_baseline_required",
            "previous_state": "target_absent",
            "live_backup_created": False,
            "live_restore_tested": False,
        }

    return {
        "mode": "live_read_only_metadata_count_preflight",
        "status": status,
        "source": source,
        "target": target,
        "count_reconciliation": {
            "source_row_count": source["row_count"],
            "target_row_count": target["row_count"],
            "difference": (
                source["row_count"] - target["row_count"]
                if target["row_count"] is not None
                else None
            ),
        },
        "backup_restore_readiness": backup_restore_readiness,
        "requires_user_approval_for_persistent_action": True,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_products_ods_backup_restore_plan(
    preflight: Mapping[str, Any],
    *,
    artifact_id: str,
) -> dict[str, Any]:
    """Build a named no-write rollback contract from reviewed preflight evidence."""
    normalized_id = str(artifact_id or "").strip().upper()
    if not _ARTIFACT_ID.fullmatch(normalized_id):
        raise ValueError("Invalid backup artifact identifier.")

    target = preflight.get("target")
    target_exists = isinstance(target, Mapping) and target.get("exists") is True
    if target_exists:
        status = "pending_named_live_backup"
        previous_state = "target_exists"
        existing_target_backup_object = f"DWH.ODS_SH_PRODUCTS_{normalized_id[-15:]}"
        restore_actions = [
            "verify_named_backup_metadata_and_count",
            "quarantine_failed_target_after_separate_approval",
            "restore_named_backup_after_separate_approval",
            "reconcile_restored_metadata_and_count",
        ]
    else:
        status = "static_restore_contract_ready"
        previous_state = "target_absent"
        existing_target_backup_object = None
        restore_actions = [
            "verify_target_created_by_approved_run",
            "verify_downstream_dim_not_promoted",
            "remove_new_target_after_separate_approval",
            "confirm_target_absent_by_metadata",
        ]

    return {
        "mode": "named_backup_restore_contract",
        "status": status,
        "baseline_artifact_id": normalized_id,
        "source": {"owner": "SH", "table": "PRODUCTS"},
        "target": {"owner": "DWH", "table": "ODS_SH_PRODUCTS"},
        "previous_state": previous_state,
        "existing_target_backup_object": existing_target_backup_object,
        "restore_actions": restore_actions,
        "static_contract_tested": True,
        "live_backup_created": False,
        "live_restore_tested": False,
        "requires_separate_persistent_approval": True,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def run_configured_products_ods_read_only_preflight() -> dict[str, Any]:
    """Open configured source/target connections and return only redacted evidence."""
    source_connection = None
    target_connection = None
    stage = "connect_source"
    try:
        import config

        source_connection = oracledb.connect(
            user=config.SOURCE_ORACLE_USER,
            password=config.SOURCE_ORACLE_PASSWORD,
            dsn=config.SOURCE_ORACLE_DSN,
        )
        stage = "connect_target"
        target_connection = oracledb.connect(
            user=config.TARGET_ORACLE_USER,
            password=config.TARGET_ORACLE_PASSWORD,
            dsn=config.TARGET_ORACLE_DSN,
        )
        stage = "read_only_preflight"
        return run_products_ods_read_only_preflight(source_connection, target_connection)
    except Exception as exc:
        return {
            "mode": "live_read_only_metadata_count_preflight",
            "status": "failed",
            "stage": stage,
            "error_type": type(exc).__name__,
            "will_execute": False,
            "write_attempted": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
        }
    finally:
        for connection in (source_connection, target_connection):
            if connection is not None:
                try:
                    connection.close()
                except oracledb.Error as exc:
                    logger.warning("Preflight connection close failed: %s", type(exc).__name__)
