"""Count-only SCD2 candidate preflight for the ODI-68 DIM_PRODUCT flow."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from dim_product_scd2_preflight import (
    run_configured_dim_product_scd2_read_only_preflight,
)
from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

SOURCE_OWNER = "DWH"
SOURCE_TABLE = "ODS_SH_PRODUCTS"
TARGET_OWNER = "DWH"
TARGET_TABLE = "DIM_PRODUCT"
SEQUENCE_NAME = "SEQ_DIM_PRODUCT"
ODS_BACKUP_TABLE = "ODI68_ODS_PRODUCTS_BKP"

_STATIC_PLAN = build_dim_product_scd2_plan(
    {
        "name": name,
        "data_type": data_type,
        "nullable": nullable,
    }
    for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
)
DIM_PRODUCT_TRACKED_COLUMNS = tuple(
    str(name) for name in _STATIC_PLAN["tracked_attributes"]
)
logger = logging.getLogger("dwh_app.dim_product_scd2_candidate_preflight")


def _base(status: str, *, connection_attempted: bool = False) -> dict[str, Any]:
    return {
        "mode": "dim_product_scd2_candidate_preflight",
        "status": status,
        "requires_user_approval_for_persistent_action": True,
        "connection_attempted": connection_attempted,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _metadata_ready(preflight: Mapping[str, Any]) -> bool:
    source = preflight.get("source")
    target = preflight.get("target")
    sequence = preflight.get("sequence")
    return (
        preflight.get("status") == "ready_for_scd2_candidate_preflight"
        and isinstance(source, Mapping)
        and source.get("exists") is True
        and source.get("metadata_status") == "metadata_ready_for_review"
        and source.get("duplicate_key_group_count") == 0
        and isinstance(target, Mapping)
        and target.get("exists") is True
        and target.get("metadata_status") == "metadata_ready_for_review"
        and target.get("duplicate_product_sk_group_count") == 0
        and target.get("duplicate_current_natural_key_group_count") == 0
        and target.get("invalid_current_flag_count") == 0
        and target.get("invalid_effective_date_order_count") == 0
        and isinstance(sequence, Mapping)
        and sequence.get("exists") is True
        and preflight.get("write_attempted") is False
        and preflight.get("raw_rows_included") is False
        and preflight.get("pk_values_included") is False
        and preflight.get("secret_printed") is False
    )


def _change_predicate() -> str:
    return " OR ".join(
        f'DECODE(t."{column}", s."{column}", 0, 1) = 1'
        for column in DIM_PRODUCT_TRACKED_COLUMNS
    )


def _scalar(cursor: Any, marker: str, sql: str, params: Mapping[str, object] | None = None) -> int:
    cursor.execute(f"/* odi68:{marker} */ {sql}", params)
    return int(cursor.fetchone()[0])


def collect_dim_product_scd2_candidate_evidence(cursor: Any) -> dict[str, int]:
    """Return only aggregate classification and DQ counts from fixed objects."""
    predicate = _change_predicate()
    evidence = {
        "source_count": _scalar(
            cursor,
            "source_count",
            f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}"',
        ),
        "target_count": _scalar(
            cursor,
            "target_count",
            f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{TARGET_TABLE}"',
        ),
        "source_duplicate_groups": _scalar(
            cursor,
            "source_duplicate_groups",
            f'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" '
            'GROUP BY "PROD_ID" HAVING COUNT(*) > 1)',
        ),
        "duplicate_product_sk_groups": _scalar(
            cursor,
            "duplicate_product_sk_groups",
            f'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "{TARGET_OWNER}"."{TARGET_TABLE}" '
            'GROUP BY "PRODUCT_SK" HAVING COUNT(*) > 1)',
        ),
        "duplicate_current_natural_key_groups": _scalar(
            cursor,
            "duplicate_current_natural_key_groups",
            f'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "{TARGET_OWNER}"."{TARGET_TABLE}" '
            'WHERE "CURRENT_FLAG" = \'Y\' GROUP BY "PROD_ID" HAVING COUNT(*) > 1)',
        ),
        "invalid_current_flags": _scalar(
            cursor,
            "invalid_current_flags",
            f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{TARGET_TABLE}" '
            'WHERE "CURRENT_FLAG" NOT IN (\'Y\', \'N\') OR "CURRENT_FLAG" IS NULL',
        ),
        "invalid_effective_dates": _scalar(
            cursor,
            "invalid_effective_dates",
            f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{TARGET_TABLE}" '
            'WHERE "EFFECTIVE_FROM" > "EFFECTIVE_TO"',
        ),
        "current_target_count": _scalar(
            cursor,
            "current_target_count",
            f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{TARGET_TABLE}" '
            'WHERE "CURRENT_FLAG" = \'Y\'',
        ),
        "insert_without_current_count": _scalar(
            cursor,
            "insert_without_current_count",
            f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
            f'LEFT JOIN "{TARGET_OWNER}"."{TARGET_TABLE}" t '
            'ON t."PROD_ID" = s."PROD_ID" AND t."CURRENT_FLAG" = \'Y\' '
            'WHERE t."PROD_ID" IS NULL',
        ),
        "changed_current_count": _scalar(
            cursor,
            "changed_current_count",
            f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
            f'JOIN "{TARGET_OWNER}"."{TARGET_TABLE}" t '
            'ON t."PROD_ID" = s."PROD_ID" AND t."CURRENT_FLAG" = \'Y\' '
            f'WHERE {predicate}',
        ),
        "unchanged_current_count": _scalar(
            cursor,
            "unchanged_current_count",
            f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
            f'JOIN "{TARGET_OWNER}"."{TARGET_TABLE}" t '
            'ON t."PROD_ID" = s."PROD_ID" AND t."CURRENT_FLAG" = \'Y\' '
            f'WHERE NOT ({predicate})',
        ),
        "source_missing_current_count": _scalar(
            cursor,
            "source_missing_current_count",
            f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{TARGET_TABLE}" t '
            'WHERE t."CURRENT_FLAG" = \'Y\' AND NOT EXISTS ('
            f'SELECT 1 FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
            'WHERE s."PROD_ID" = t."PROD_ID")',
        ),
    }
    evidence["backup_exists"] = _scalar(
        cursor,
        "backup_exists",
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": TARGET_OWNER, "table_name": ODS_BACKUP_TABLE},
    )
    if evidence["backup_exists"] == 1:
        evidence["backup_column_count"] = _scalar(
            cursor,
            "backup_column_count",
            "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name",
            {"owner": TARGET_OWNER, "table_name": ODS_BACKUP_TABLE},
        )
        evidence["backup_row_count"] = _scalar(
            cursor,
            "backup_row_count",
            f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{ODS_BACKUP_TABLE}"',
        )
    else:
        evidence["backup_column_count"] = 0
        evidence["backup_row_count"] = 0
    evidence["candidate_count"] = (
        evidence["insert_without_current_count"] + evidence["changed_current_count"]
    )
    return evidence


def candidate_status(evidence: Mapping[str, int]) -> str:
    if evidence["source_duplicate_groups"] != 0:
        return "blocked_source_duplicate_keys"
    if evidence["duplicate_product_sk_groups"] != 0:
        return "blocked_target_duplicate_surrogate_keys"
    if evidence["duplicate_current_natural_key_groups"] != 0:
        return "blocked_target_multiple_current_rows"
    if evidence["invalid_current_flags"] != 0:
        return "blocked_target_invalid_current_flags"
    if evidence["invalid_effective_dates"] != 0:
        return "blocked_target_invalid_effective_dates"
    if evidence["source_missing_current_count"] != 0:
        return "blocked_delete_policy_required"
    if evidence["backup_exists"] != 1:
        return "blocked_ods_backup_missing"
    if evidence["backup_column_count"] != 24:
        return "blocked_ods_backup_metadata"
    if evidence["backup_row_count"] != 0:
        return "blocked_ods_backup_count"
    classified = (
        evidence["insert_without_current_count"]
        + evidence["changed_current_count"]
        + evidence["unchanged_current_count"]
    )
    if classified != evidence["source_count"]:
        return "blocked_candidate_count_mismatch"
    if evidence["candidate_count"] == 0:
        return "rerun_noop"
    return "ready_for_scd2_load_review"


def _close(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def run_dim_product_scd2_candidate_preflight(
    metadata_preflight: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Classify SCD2 candidates in a read-only transaction without returning keys."""
    if not _metadata_ready(metadata_preflight):
        return _base("blocked_metadata_preflight")

    connection = None
    cursor = None
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "begin_read_only"
        cursor.execute("SET TRANSACTION READ ONLY")
        stage = "count_candidates"
        evidence = collect_dim_product_scd2_candidate_evidence(cursor)
        return {
            **_base(candidate_status(evidence), connection_attempted=True),
            **evidence,
        }
    except (oracledb.Error, TypeError, ValueError) as exc:
        return {
            **_base("failed", connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
        }
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except oracledb.Error as exc:
                logger.warning("Candidate preflight rollback failed: %s", type(exc).__name__)
        _close(cursor, "cursor")
        _close(connection, "connection")


def _configured_connection() -> Any:
    import config

    return oracledb.connect(
        user=config.TARGET_ORACLE_USER,
        password=config.TARGET_ORACLE_PASSWORD,
        dsn=config.TARGET_ORACLE_DSN,
    )


def run_configured_dim_product_scd2_candidate_preflight() -> dict[str, Any]:
    """Run both live checks and expose only redacted count/status/metadata evidence."""
    metadata = run_configured_dim_product_scd2_read_only_preflight()
    if metadata.get("status") != "ready_for_scd2_candidate_preflight":
        return {
            **_base("blocked_metadata_preflight"),
            "metadata_preflight_status": metadata.get("status", "failed"),
        }
    try:
        return run_dim_product_scd2_candidate_preflight(
            metadata,
            connection_factory=_configured_connection,
        )
    except (ImportError, AttributeError, TypeError, ValueError, oracledb.Error) as exc:
        return {
            **_base("failed"),
            "stage": "configured_candidate_preflight",
            "error_type": type(exc).__name__,
        }
