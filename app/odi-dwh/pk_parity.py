"""Read-only PK parity checks for source-to-ODS validation.

This module reads only row counts and primary-key columns. It never returns raw
rows or primary-key values.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pandas as pd

from db import (
    execute_select,
    execute_source_select,
    get_source_pk_columns,
    get_source_table_schema,
    get_table_constraints,
    get_table_schema,
)
from flow_inventory import get_ods_flow_inventory


_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_$]{0,127}$")


def _clean_identifier(value: str, field_name: str) -> str:
    text = str(value or "").strip().upper()
    if not _IDENTIFIER_RE.match(text):
        raise ValueError(f"Invalid {field_name}.")
    return text


def _split_table_ref(table_ref: str) -> tuple[str, str]:
    text = str(table_ref or "").strip().upper()
    if "." not in text:
        raise ValueError("Table reference must include owner.")
    owner, table = text.split(".", 1)
    return _clean_identifier(owner, "owner"), _clean_identifier(table, "table")


def _quote_table(table_ref: str) -> str:
    owner, table = _split_table_ref(table_ref)
    return f'"{owner}"."{table}"'


def _quote_column(column_name: str) -> str:
    return f'"{_clean_identifier(column_name, "column")}"'


def _first_count(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    return int(df.iloc[0, 0])


def _count_rows(table_ref: str, executor) -> int:
    df = executor(f"SELECT COUNT(*) AS ROW_COUNT FROM {_quote_table(table_ref)}")
    return _first_count(df)


def _target_pk_columns(table_ref: str) -> list[str]:
    constraints = get_table_constraints(table_ref)
    if constraints.empty:
        return []
    rows = constraints.to_dict(orient="records")
    return [
        _clean_identifier(row.get("COLUMN_NAME"), "target_pk_column")
        for row in rows
        if str(row.get("CONSTRAINT_TYPE") or "").upper() == "P"
    ]


def _target_columns(table_ref: str) -> set[str]:
    schema = get_table_schema(table_ref)
    if schema.empty or "COLUMN_NAME" not in schema.columns:
        return set()
    return {_clean_identifier(value, "target_column") for value in schema["COLUMN_NAME"].tolist()}


def _source_columns(table_ref: str) -> set[str]:
    schema = get_source_table_schema(table_ref)
    if schema.empty or "COLUMN_NAME" not in schema.columns:
        return set()
    return {_clean_identifier(value, "source_column") for value in schema["COLUMN_NAME"].tolist()}


def _configured_key_columns(source_table: str, target_table: str) -> list[str]:
    from golden_record_regression import default_specs

    source = source_table.upper()
    target = target_table.upper()
    for spec in default_specs().values():
        if spec.source_table.upper() == source and spec.target_table.upper() == target:
            return [_clean_identifier(column, "configured_key_column") for column in spec.key_columns]
    return []


def _resolve_pk_columns(source_table: str, target_table: str) -> tuple[list[str], str | None]:
    source_pk = [_clean_identifier(col, "source_pk_column") for col in get_source_pk_columns(source_table)]
    if not source_pk:
        configured_key = _configured_key_columns(source_table, target_table)
        if not configured_key:
            return [], "source_pk_missing"
        if not set(configured_key).issubset(_source_columns(source_table)):
            return [], "configured_key_columns_missing_in_source"
        source_pk = configured_key

    target_pk = _target_pk_columns(target_table)
    if target_pk:
        if target_pk == source_pk:
            return source_pk, None
        return [], "pk_columns_mismatch"

    target_columns = _target_columns(target_table)
    if set(source_pk).issubset(target_columns):
        return source_pk, None
    return [], "target_pk_missing"


def _pk_fingerprints(table_ref: str, pk_columns: list[str], executor, max_keys: int) -> set[str]:
    select_cols = ", ".join(_quote_column(col) for col in pk_columns)
    limit = int(max_keys) + 1
    df = executor(f"SELECT {select_cols} FROM {_quote_table(table_ref)} FETCH FIRST {limit} ROWS ONLY")
    if len(df.index) > max_keys:
        raise ValueError("pk_compare_key_limit_exceeded")

    fingerprints: set[str] = set()
    for row in df.to_dict(orient="records"):
        values = [str(row.get(col) if row.get(col) is not None else "<NULL>") for col in pk_columns]
        payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
        fingerprints.add(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return fingerprints


def _blocked_result(
    row: dict[str, Any],
    blocker: str,
    *,
    pk_columns: list[str] | None = None,
    source_row_count: int | None = None,
    target_row_count: int | None = None,
    count_strategy: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blocker": blocker,
        "source_table": row.get("SOURCE_TABLE"),
        "target_table": row.get("EXPECTED_TARGET_TABLE"),
        "mapping_name": row.get("MAPPING_NAME") or None,
        "pk_columns": pk_columns or [],
        "source_row_count": source_row_count,
        "target_row_count": target_row_count,
        "missing_key_count": None,
        "extra_key_count": None,
        "count_strategy": count_strategy,
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def _pk_projection_sql(table_ref: str, pk_columns: list[str]) -> str:
    select_cols = ", ".join(_quote_column(col) for col in pk_columns)
    return f"SELECT {select_cols} FROM {_quote_table(table_ref)}"


def _anti_join_count(left_table: str, right_table: str, pk_columns: list[str], executor) -> int:
    sql = (
        "SELECT COUNT(*) AS KEY_COUNT FROM ("
        f"{_pk_projection_sql(left_table, pk_columns)} "
        "MINUS "
        f"{_pk_projection_sql(right_table, pk_columns)}"
        ")"
    )
    return _first_count(executor(sql))


def _anti_join_counts(source_table: str, target_table: str, pk_columns: list[str], executor) -> tuple[int, int]:
    missing_count = _anti_join_count(source_table, target_table, pk_columns, executor)
    extra_count = _anti_join_count(target_table, source_table, pk_columns, executor)
    return missing_count, extra_count


def _check_row(row: dict[str, Any], max_keys: int) -> dict[str, Any]:
    source_table = str(row.get("SOURCE_TABLE") or "").upper()
    target_table = str(row.get("EXPECTED_TARGET_TABLE") or "").upper()

    if str(row.get("ODI_MAPPING_EXISTS") or "").upper() not in {"TRUE", "1", "Y"} and row.get("ODI_MAPPING_EXISTS") is not True:
        return _blocked_result(row, "mapping_not_ready")
    if str(row.get("TARGET_TABLE_EXISTS") or "").upper() in {"FALSE", "0", "N"} or row.get("TARGET_TABLE_EXISTS") is False:
        return _blocked_result(row, "target_table_missing")

    pk_columns, blocker = _resolve_pk_columns(source_table, target_table)
    if blocker:
        return _blocked_result(row, blocker)

    source_count = _count_rows(source_table, execute_source_select)
    target_count = _count_rows(target_table, execute_select)
    if source_count > max_keys or target_count > max_keys:
        try:
            missing_count, extra_count = _anti_join_counts(source_table, target_table, pk_columns, execute_select)
        except Exception:
            return _blocked_result(
                row,
                "shared_read_connection_unavailable",
                pk_columns=pk_columns,
                source_row_count=source_count,
                target_row_count=target_count,
                count_strategy="in_db_anti_join",
            )
        passed = source_count == target_count and missing_count == 0 and extra_count == 0
        return {
            "status": "passed" if passed else "failed",
            "source_table": source_table,
            "target_table": target_table,
            "mapping_name": row.get("MAPPING_NAME") or None,
            "pk_columns": pk_columns,
            "source_row_count": source_count,
            "target_row_count": target_count,
            "row_count_matched": source_count == target_count,
            "source_distinct_pk_count": None,
            "target_distinct_pk_count": None,
            "missing_key_count": missing_count,
            "extra_key_count": extra_count,
            "count_strategy": "in_db_anti_join",
            "raw_rows_included": False,
            "pk_values_included": False,
        }

    source_keys = _pk_fingerprints(source_table, pk_columns, execute_source_select, max_keys)
    target_keys = _pk_fingerprints(target_table, pk_columns, execute_select, max_keys)
    missing_count = len(source_keys - target_keys)
    extra_count = len(target_keys - source_keys)
    passed = source_count == target_count and missing_count == 0 and extra_count == 0

    return {
        "status": "passed" if passed else "failed",
        "source_table": source_table,
        "target_table": target_table,
        "mapping_name": row.get("MAPPING_NAME") or None,
        "pk_columns": pk_columns,
        "source_row_count": source_count,
        "target_row_count": target_count,
        "row_count_matched": source_count == target_count,
        "source_distinct_pk_count": len(source_keys),
        "target_distinct_pk_count": len(target_keys),
        "missing_key_count": missing_count,
        "extra_key_count": extra_count,
        "count_strategy": "client_hash_set",
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def build_pk_parity_report(source_schema: str = "HR", target_owner: str = "DWH", *, max_keys: int = 100000) -> dict[str, Any]:
    """Compare source and expected ODS target PK sets without returning values."""
    source_schema = _clean_identifier(source_schema, "source_schema")
    target_owner = _clean_identifier(target_owner, "target_owner")
    if max_keys <= 0:
        raise ValueError("max_keys must be positive.")

    flow_rows = get_ods_flow_inventory(source_schema=source_schema, target_owner=target_owner)
    results = [_check_row(row, max_keys=max_keys) for row in flow_rows]
    passed = bool(results) and all(row.get("status") == "passed" for row in results)
    return {
        "source_schema": source_schema,
        "target_owner": target_owner,
        "status": "passed" if passed else "attention_required",
        "result_count": len(results),
        "results": results,
        "read_only": True,
        "scope": "source_to_target_pk_parity",
        "raw_rows_included": False,
        "pk_values_included": False,
        "max_keys": max_keys,
    }
