"""Static, metadata-only audit schema contract and preflight plan."""

from __future__ import annotations

from typing import Any


AUDIT_SCHEMA_OWNER = "AUDIT_USER"
AUDIT_SCHEMA_TABLES: dict[str, tuple[str, ...]] = {
    "ALERT_LOG": (
        "ALERT_ID", "ALERT_TYPE", "SOURCE", "MESSAGE", "TABLE_OWNER", "TABLE_NAME",
        "DETAILS", "SEVERITY", "ACKNOWLEDGED", "ACKNOWLEDGED_AT", "CREATED_AT",
    ),
    "MAPPING_LOG": (
        "LOG_ID", "BATCH_ID", "OPERATION", "OBJECT_OWNER", "OBJECT_NAME", "DDL_TEXT",
        "STATUS", "ERROR_MESSAGE", "CREATED_AT",
    ),
    "DATA_LINEAGE": (
        "LINEAGE_ID", "MAPPING_NAME", "SOURCE_OWNER", "SOURCE_TABLE", "SOURCE_COLUMN",
        "TARGET_OWNER", "TARGET_TABLE", "TARGET_COLUMN", "EXPRESSION", "UPDATED_AT",
    ),
    "MAPPING_SNAPSHOT": (
        "SNAP_ID", "MAPPING_NAME", "SNAP_DATE", "SNAP_LABEL", "SNAP_HASH", "SNAP_JSON",
    ),
    "MAPPING_RUN_STATE": (
        "RUN_ID", "BATCH_ID", "THREAD_ID", "MAPPING_NAME", "CURRENT_STEP", "OVERALL_STATUS",
        "COMPLETED_STEPS", "ERROR_MESSAGE", "STATE_JSON", "CREATED_AT", "UPDATED_AT",
    ),
}


def build_audit_schema_preflight_plan() -> dict[str, Any]:
    """Return the read-only metadata queries required before a reviewed migration."""
    return {
        "status": "static_metadata_plan",
        "owner": AUDIT_SCHEMA_OWNER,
        "required_tables": AUDIT_SCHEMA_TABLES,
        "queries": {
            "columns": (
                "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE "
                "FROM ALL_TAB_COLUMNS WHERE OWNER = :owner ORDER BY TABLE_NAME, COLUMN_ID"
            ),
            "indexes": (
                "SELECT TABLE_NAME, INDEX_NAME, UNIQUENESS FROM ALL_INDEXES "
                "WHERE OWNER = :owner ORDER BY TABLE_NAME, INDEX_NAME"
            ),
            "grants": (
                "SELECT TABLE_NAME, GRANTEE, PRIVILEGE FROM ALL_TAB_PRIVS "
                "WHERE OWNER = :owner ORDER BY TABLE_NAME, GRANTEE, PRIVILEGE"
            ),
        },
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "live_proven": False,
    }
