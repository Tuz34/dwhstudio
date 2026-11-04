from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import oracledb

from config import SOURCE_ORACLE_SCHEMAS
from db import get_connection
from reconciliation_autofix import AutofixDependencies
from reconciliation_autofix import autofix_all_missing_columns as _autofix_all_missing_columns_service
from reconciliation_autofix import autofix_missing_column as _autofix_missing_column_service
from reconciliation_autofix import sync_odi_mapping as _sync_odi_mapping_service
from reconciliation_service import ReconciliationDependencies
from reconciliation_service import compare_data_by_pk as _compare_data_by_pk_service
from reconciliation_service import reconcile_table as _reconcile_table_service
from source_connector import get_source_connector

logger = logging.getLogger(__name__)
DATA_COMPARE_LIMIT = 500
DATETIME_TOLERANCE_SECONDS = 1

TECH_COLS = {
    "SKEY",
    "NK",
    "EFF_FROM_DT",
    "EFF_TO_DT",
    "CURRENT_FLAG",
    "INSERT_DT",
    "UPDATE_DT",
    "INSERT_DATE",
    "UPDATE_DATE",
    "SOURCE_SYSTEM",
    "ETL_BATCH_ID",
    "ETL_LOAD_TYPE",
}


def _close_oracle_resource(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except (AttributeError, oracledb.Error) as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def _source_from_state(target_table: str) -> tuple[str, str] | None:
    try:
        from mapping_state_manager import MappingStateManager
    except ImportError:
        return None
    try:
        with MappingStateManager() as manager:
            state = manager.get_by_target_table(target_table)
    except Exception as exc:
        logger.debug("Mapping state source lookup skipped: %s", type(exc).__name__)
        return None
    if not state:
        return None
    params = state.get("validated_params") or state.get("raw_params") or {}
    source_schema = str(params.get("src_schema") or "").strip()
    source_table = str(params.get("src_table") or "").strip()
    if source_table and "." in source_table and not source_schema:
        source_schema, source_table = (part.strip() for part in source_table.split(".", 1))
    if not source_table:
        combined = str(params.get("src_table_full") or params.get("src") or "").strip()
        if "." in combined:
            source_schema, source_table = (part.strip() for part in combined.split(".", 1))
    if not source_table:
        return None
    return source_schema or (SOURCE_ORACLE_SCHEMAS[0] if SOURCE_ORACLE_SCHEMAS else "HR"), source_table


def _parse_source_table(target_table: str) -> tuple[str, str]:
    state_source = _source_from_state(target_table)
    if state_source:
        return state_source
    parts = target_table.replace("ODS_", "", 1).split("_", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("dbo", parts[0])


def _map_sql_to_oracle_type(sql_type: str) -> str:
    normalized = sql_type.lower()
    if normalized in {
        "int", "bigint", "smallint", "tinyint", "numeric", "decimal", "money",
        "smallmoney", "float", "real", "bit",
    }:
        return "NUMBER"
    if normalized in {"varchar", "nvarchar", "char", "nchar", "text", "ntext"}:
        return "VARCHAR2"
    if normalized in {"datetime", "date", "smalldatetime", "datetime2", "time"}:
        return "DATE"
    if normalized == "uniqueidentifier":
        return "VARCHAR2"
    return normalized.upper()


def _source_name(source_schema: str, source_table: str) -> str:
    return f"{source_schema.upper()}.{source_table.upper()}"


def _source_schema_columns(source_schema: str, source_table: str) -> dict[str, dict[str, Any]]:
    connector = get_source_connector()
    schema_frame = connector.get_table_schema(_source_name(source_schema, source_table))
    columns: dict[str, dict[str, Any]] = {}
    for _, row in schema_frame.iterrows():
        name = str(row.get("COLUMN_NAME") or "").upper()
        if not name:
            continue
        full_type = row.get("FULL_DATA_TYPE") or row.get("DATA_TYPE")
        columns[name] = {
            "type": str(row.get("DATA_TYPE") or "").upper(),
            "oracle_type": connector.type_to_oracle(
                full_type,
                row.get("DATA_LENGTH"),
                row.get("DATA_PRECISION"),
                row.get("DATA_SCALE"),
            ),
            "len": row.get("DATA_LENGTH"),
            "prec": row.get("DATA_PRECISION"),
            "scale": row.get("DATA_SCALE"),
            "nullable": row.get("NULLABLE"),
        }
    return columns


def _source_count(source_schema: str, source_table: str) -> int:
    connector = get_source_connector()
    if connector.db_type == "mssql":
        sql = f"SELECT COUNT(*) AS CNT FROM [{source_schema}].[{source_table}]"
    else:
        sql = f'SELECT COUNT(*) AS CNT FROM "{source_schema.upper()}"."{source_table.upper()}"'
    frame = connector.execute_select(sql)
    return int(frame.iloc[0]["CNT"])


def _source_pk_columns(source_schema: str, source_table: str) -> list[str]:
    connector = get_source_connector()
    return [column.upper() for column in connector.get_pk_columns(_source_name(source_schema, source_table))]


def _q_mssql(name: str) -> str:
    return "[" + str(name).replace("]", "") + "]"


def _q_oracle(name: str) -> str:
    return '"' + str(name).upper().replace('"', "") + '"'


def _source_sample_rows(
    source_schema: str,
    source_table: str,
    pk_columns: list[str],
    compare_columns: list[str],
):
    connector = get_source_connector()
    columns = [*pk_columns, *compare_columns]
    if connector.db_type == "mssql":
        selected = ", ".join(_q_mssql(column) for column in columns)
        ordered = ", ".join(_q_mssql(column) for column in pk_columns)
        sql = (
            f"SELECT TOP {DATA_COMPARE_LIMIT} {selected} "
            f"FROM [{source_schema}].[{source_table}] ORDER BY {ordered}"
        )
    else:
        selected = ", ".join(_q_oracle(column) for column in columns)
        ordered = ", ".join(_q_oracle(column) for column in pk_columns)
        sql = (
            f'SELECT {selected} FROM "{source_schema.upper()}"."{source_table.upper()}" '
            f"ORDER BY {ordered} FETCH FIRST {DATA_COMPARE_LIMIT} ROWS ONLY"
        )
    return connector.execute_select(sql)


def _norm_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _is_datetime_like(value: Any) -> bool:
    return isinstance(value, (datetime, date)) or hasattr(value, "isoformat")


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _type_family(props: dict[str, Any]) -> str:
    type_text = str(props.get("oracle_type") or props.get("type") or "").upper()
    if "TIMESTAMP" in type_text:
        return "TIMESTAMP"
    if "DATE" in type_text:
        return "DATE"
    if any(name in type_text for name in ("NUMBER", "FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT")):
        return "NUMBER"
    if "CHAR" in type_text or "CLOB" in type_text:
        return "VARCHAR2"
    if "BLOB" in type_text or "RAW" in type_text:
        return "BINARY"
    return str(props.get("type") or "").upper()


def _is_lob_type(props: dict[str, Any] | None) -> bool:
    if not props:
        return False
    type_text = " ".join(str(props.get(key) or "") for key in ("type", "oracle_type")).upper()
    return any(name in type_text for name in ("CLOB", "NCLOB", "BLOB", "LONG", "RAW"))


def _values_equal(
    source_value: Any,
    target_value: Any,
    source_props: dict[str, Any] | None = None,
    target_props: dict[str, Any] | None = None,
) -> bool:
    source, target = _norm_value(source_value), _norm_value(target_value)
    if source is None or target is None:
        return source is target
    if (
        _is_datetime_like(source_value)
        or _is_datetime_like(target_value)
        or _type_family(source_props or {}) in {"DATE", "TIMESTAMP"}
    ):
        source_date, target_date = _to_datetime(source_value), _to_datetime(target_value)
        if source_date and target_date:
            return abs((source_date - target_date).total_seconds()) <= DATETIME_TOLERANCE_SECONDS
    if isinstance(source_value, Decimal) or isinstance(target_value, Decimal):
        try:
            return Decimal(str(source_value)) == Decimal(str(target_value))
        except ArithmeticError:
            return False
    return str(source).casefold() == str(target).casefold()


def _pk_key(row: dict[str, Any], pk_columns: list[str]) -> str:
    return "|".join(str(_norm_value(row.get(column)) or "") for column in pk_columns)


def _infer_pk_columns(
    source_schema: str,
    source_table: str,
    source_columns: dict[str, Any],
    target_columns: dict[str, Any],
) -> list[str]:
    try:
        detected = [column for column in _source_pk_columns(source_schema, source_table) if column in target_columns]
        if detected:
            return detected
    except Exception as exc:
        logger.warning("Source PK detection skipped: %s", type(exc).__name__)
    candidates = [f"{source_table.upper()}ID", f"{source_table.upper()}_ID", "ID"]
    for column in source_columns:
        if column in target_columns and column in candidates:
            return [column]
    for column in source_columns:
        if column in target_columns and column.endswith("ID"):
            return [column]
    return []


def _reconciliation_dependencies() -> ReconciliationDependencies:
    return ReconciliationDependencies(
        get_connection=get_connection,
        close_resource=_close_oracle_resource,
        parse_source_table=_parse_source_table,
        source_schema_columns=_source_schema_columns,
        source_count=_source_count,
        source_sample_rows=_source_sample_rows,
        infer_pk_columns=_infer_pk_columns,
        q_oracle=_q_oracle,
        pk_key=_pk_key,
        is_lob_type=_is_lob_type,
        norm_value=_norm_value,
        values_equal=_values_equal,
        map_sql_to_oracle_type=_map_sql_to_oracle_type,
        compare_data=_compare_data_by_pk,
        tech_columns=TECH_COLS,
        compare_limit=DATA_COMPARE_LIMIT,
    )


def _compare_data_by_pk(
    owner: str,
    table: str,
    source_schema: str,
    source_table: str,
    source_columns: dict[str, Any],
    target_columns: dict[str, Any],
) -> dict[str, Any]:
    """Run metadata-only sampled comparison without returning row keys or values."""
    return _compare_data_by_pk_service(
        owner,
        table,
        source_schema,
        source_table,
        source_columns,
        target_columns,
        _reconciliation_dependencies(),
    )


def reconcile_table(owner: str, table: str) -> dict[str, Any]:
    """Compare one target with its source without persisting evidence."""
    owner = str(owner or "").strip().upper()
    table = str(table or "").strip().upper()
    if "." in table:
        owner, table = table.split(".", 1)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", owner) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", table):
        raise ValueError("Invalid reconciliation identifier")
    return _reconcile_table_service(owner, table, _reconciliation_dependencies())


def _autofix_dependencies() -> AutofixDependencies:
    return AutofixDependencies(
        source_schema_columns=_source_schema_columns,
        map_sql_to_oracle_type=_map_sql_to_oracle_type,
        get_connection=get_connection,
        close_resource=_close_oracle_resource,
        sync_mapping=sync_odi_mapping,
    )


def autofix_missing_column(owner: str, table: str, column: str) -> dict[str, Any]:
    return _autofix_missing_column_service(owner, table, column, _autofix_dependencies())


def autofix_all_missing_columns(owner: str, table: str, columns: list[str]) -> dict[str, Any]:
    return _autofix_all_missing_columns_service(owner, table, columns, _autofix_dependencies())


def sync_odi_mapping(owner: str, table: str) -> dict[str, Any]:
    return _sync_odi_mapping_service(owner, table)
