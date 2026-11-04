"""Read-only live preflight for the ODI-68 DIM_PRODUCT SCD2 flow."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

import oracledb

from scd2_manager import build_dim_product_scd2_plan

_SOURCE = ("DWH", "ODS_SH_PRODUCTS")
_TARGET = ("DWH", "DIM_PRODUCT")
_SEQUENCE = ("DWH", "SEQ_DIM_PRODUCT")
_ALLOWED_TABLES = {_SOURCE, _TARGET}
_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]*$")
logger = logging.getLogger("dwh_app.dim_product_scd2_preflight")


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


def _read_table_metadata(cursor: Any, owner: str, table: str) -> list[dict[str, Any]]:
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
    return [
        {
            "name": str(name),
            "data_type": _full_oracle_type(data_type, data_length, precision, scale),
            "nullable": nullable == "Y",
        }
        for name, data_type, data_length, precision, scale, nullable in cursor.fetchall()
    ]


def _count_table(cursor: Any, owner: str, table: str) -> int:
    if (owner, table) not in _ALLOWED_TABLES:
        raise ValueError("Unreviewed preflight table.")
    cursor.execute(f'SELECT COUNT(*) FROM "{owner}"."{table}"')
    return int(cursor.fetchone()[0])


def _source_duplicate_count(cursor: Any) -> int:
    cursor.execute(
        'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "DWH"."ODS_SH_PRODUCTS" '
        'GROUP BY "PROD_ID" HAVING COUNT(*) > 1)'
    )
    return int(cursor.fetchone()[0])


def _target_dq_counts(cursor: Any) -> dict[str, int]:
    cursor.execute(
        'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."DIM_PRODUCT" '
        'GROUP BY "PRODUCT_SK" HAVING COUNT(*) > 1)'
    )
    duplicate_product_sk = int(cursor.fetchone()[0])
    cursor.execute(
        'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "DWH"."DIM_PRODUCT" '
        'WHERE "CURRENT_FLAG" = \'Y\' GROUP BY "PROD_ID" HAVING COUNT(*) > 1)'
    )
    duplicate_current_natural_key = int(cursor.fetchone()[0])
    cursor.execute(
        'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" '
        'WHERE "CURRENT_FLAG" NOT IN (\'Y\', \'N\') OR "CURRENT_FLAG" IS NULL'
    )
    invalid_current_flag = int(cursor.fetchone()[0])
    cursor.execute(
        'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" '
        'WHERE "EFFECTIVE_FROM" > "EFFECTIVE_TO"'
    )
    invalid_effective_date_order = int(cursor.fetchone()[0])
    return {
        "duplicate_product_sk_group_count": duplicate_product_sk,
        "duplicate_current_natural_key_group_count": duplicate_current_natural_key,
        "invalid_current_flag_count": invalid_current_flag,
        "invalid_effective_date_order_count": invalid_effective_date_order,
    }


def _sequence_snapshot(cursor: Any) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM ALL_SEQUENCES
        WHERE SEQUENCE_OWNER = :owner AND SEQUENCE_NAME = :sequence_name
        """,
        {"owner": _SEQUENCE[0], "sequence_name": _SEQUENCE[1]},
    )
    return {
        "owner": _SEQUENCE[0],
        "sequence": _SEQUENCE[1],
        "exists": int(cursor.fetchone()[0]) == 1,
    }


def _target_metadata_drift(
    metadata: Iterable[Mapping[str, object]],
    expected_columns: Iterable[Mapping[str, object]],
) -> dict[str, list[str]]:
    actual: dict[str, Mapping[str, object]] = {}
    invalid = False
    for column in metadata:
        name = str(column.get("name") or "").strip().upper()
        if not _IDENTIFIER.fullmatch(name) or name in actual:
            invalid = True
            continue
        actual[name] = column

    expected = {
        str(column["name"]): column
        for column in expected_columns
    }
    missing = [name for name in expected if name not in actual]
    unexpected = sorted(set(actual) - set(expected))
    type_mismatches = [
        name
        for name, column in expected.items()
        if name in actual and actual[name].get("data_type") != column.get("data_type")
    ]
    nullability_mismatches = [
        name
        for name, column in expected.items()
        if name in actual and actual[name].get("nullable") is not column.get("nullable")
    ]
    if invalid:
        unexpected.append("INVALID_OR_DUPLICATE_METADATA_ENTRY")
    return {
        "missing_columns": missing,
        "unexpected_columns": unexpected,
        "type_mismatches": type_mismatches,
        "nullability_mismatches": nullability_mismatches,
    }


def _empty_target_dq() -> dict[str, None]:
    return {
        "duplicate_product_sk_group_count": None,
        "duplicate_current_natural_key_group_count": None,
        "invalid_current_flag_count": None,
        "invalid_effective_date_order_count": None,
    }


def _rollback_read_only(connection: Any) -> None:
    try:
        connection.rollback()
    except oracledb.Error as exc:
        logger.warning("Read-only rollback failed: %s", type(exc).__name__)


def run_dim_product_scd2_read_only_preflight(connection: Any) -> dict[str, Any]:
    """Collect metadata, counts, and DQ statuses without returning row values."""
    cursor = connection.cursor()
    try:
        cursor.execute("SET TRANSACTION READ ONLY")
        source_metadata = _read_table_metadata(cursor, *_SOURCE)
        source = {
            "owner": _SOURCE[0],
            "table": _SOURCE[1],
            "exists": bool(source_metadata),
            "column_count": len(source_metadata),
            "metadata": source_metadata,
            "row_count": _count_table(cursor, *_SOURCE) if source_metadata else None,
            "duplicate_key_group_count": (
                _source_duplicate_count(cursor) if source_metadata else None
            ),
        }
        source_plan = build_dim_product_scd2_plan(source_metadata)
        source["metadata_status"] = source_plan["status"]
        source["metadata_drift"] = source_plan["metadata_drift"]

        target_metadata = _read_table_metadata(cursor, *_TARGET)
        target = {
            "owner": _TARGET[0],
            "table": _TARGET[1],
            "exists": bool(target_metadata),
            "column_count": len(target_metadata),
            "metadata": target_metadata,
            "row_count": _count_table(cursor, *_TARGET) if target_metadata else None,
            **_empty_target_dq(),
        }
        if target_metadata and source_plan["status"] == "metadata_ready_for_review":
            target_drift = _target_metadata_drift(
                target_metadata,
                source_plan["target_columns"],
            )
            target["metadata_drift"] = target_drift
            if any(target_drift.values()):
                target["metadata_status"] = "blocked_by_metadata_drift"
            else:
                target["metadata_status"] = "metadata_ready_for_review"
                target.update(_target_dq_counts(cursor))
        elif target_metadata:
            target["metadata_status"] = "not_evaluated_source_metadata_drift"
            target["metadata_drift"] = {
                "missing_columns": [],
                "unexpected_columns": [],
                "type_mismatches": [],
                "nullability_mismatches": [],
            }
        else:
            target["metadata_status"] = "target_missing"
            target["metadata_drift"] = {
                "missing_columns": [],
                "unexpected_columns": [],
                "type_mismatches": [],
                "nullability_mismatches": [],
            }

        sequence = _sequence_snapshot(cursor)
    finally:
        cursor.close()
        _rollback_read_only(connection)

    if not source["exists"]:
        status = "blocked_source_missing"
    elif source_plan["status"] != "metadata_ready_for_review":
        status = "blocked_source_metadata_drift"
    elif source["duplicate_key_group_count"] != 0:
        status = "blocked_source_duplicate_keys"
    elif not target["exists"]:
        status = "blocked_target_missing"
    elif target["metadata_status"] != "metadata_ready_for_review":
        status = "blocked_target_metadata_drift"
    elif target["duplicate_product_sk_group_count"] != 0:
        status = "blocked_target_duplicate_surrogate_keys"
    elif target["duplicate_current_natural_key_group_count"] != 0:
        status = "blocked_target_multiple_current_rows"
    elif target["invalid_current_flag_count"] != 0:
        status = "blocked_target_invalid_current_flags"
    elif target["invalid_effective_date_order_count"] != 0:
        status = "blocked_target_invalid_effective_dates"
    elif not sequence["exists"]:
        status = "blocked_sequence_missing"
    else:
        status = "ready_for_scd2_candidate_preflight"

    if status == "blocked_target_missing":
        required_next = ["review_static_target_and_sequence_contract"]
    elif status == "blocked_sequence_missing":
        required_next = ["review_static_sequence_contract"]
    elif status == "ready_for_scd2_candidate_preflight":
        required_next = ["build_separate_count_only_candidate_preflight"]
    else:
        required_next = ["review_preflight_blocker"]

    return {
        "mode": "live_read_only_dim_product_scd2_preflight",
        "status": status,
        "source": source,
        "target": target,
        "sequence": sequence,
        "count_reconciliation": {
            "source_row_count": source["row_count"],
            "target_row_count": target["row_count"],
        },
        "required_next": required_next,
        "requires_user_approval_for_persistent_action": True,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def run_configured_dim_product_scd2_read_only_preflight() -> dict[str, Any]:
    """Open the configured DWH connection and return only redacted evidence."""
    connection = None
    stage = "connect_target"
    try:
        import config

        connection = oracledb.connect(
            user=config.TARGET_ORACLE_USER,
            password=config.TARGET_ORACLE_PASSWORD,
            dsn=config.TARGET_ORACLE_DSN,
        )
        stage = "read_only_preflight"
        return run_dim_product_scd2_read_only_preflight(connection)
    except (ImportError, AttributeError, TypeError, ValueError, oracledb.Error) as exc:
        return {
            "mode": "live_read_only_dim_product_scd2_preflight",
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
        if connection is not None:
            try:
                connection.close()
            except oracledb.Error as exc:
                logger.warning("Preflight connection close failed: %s", type(exc).__name__)
