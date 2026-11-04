from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import oracledb

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutofixDependencies:
    source_schema_columns: Callable[[str, str], dict[str, dict[str, Any]]]
    map_sql_to_oracle_type: Callable[[str], str]
    get_connection: Callable[[], Any]
    close_resource: Callable[[Any, str], None]
    sync_mapping: Callable[[str, str], dict[str, Any]]


def _target_source(table: str) -> tuple[str, str, str] | None:
    clean_table = str(table or "").strip().upper().split(".")[-1]
    match = re.fullmatch(r"ODS_([A-Z0-9_$#]+)_([A-Z0-9_$#]+)", clean_table)
    if not match:
        return None
    return clean_table, match.group(1), match.group(2)


def _column_declaration(column: str, props: dict[str, Any], map_type: Callable[[str], str]) -> str:
    oracle_type = map_type(str(props.get("type") or "VARCHAR"))
    if oracle_type == "VARCHAR2":
        length = props.get("len")
        if not length or length == -1:
            length = 4000
        oracle_type = f"VARCHAR2({length} CHAR)"
    elif oracle_type == "NUMBER":
        oracle_type = "NUMBER"
    return f'"{column}" {oracle_type}'


_COLUMN_DDL_FAILED = "Column DDL failed."
_COLUMN_DDL_NOT_APPLIED = "Column DDL was not applied."


def _rollback_autofix(connection: Any) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except (AttributeError, oracledb.Error) as rollback_exc:
        logger.warning("Autofix rollback failed: %s", type(rollback_exc).__name__)


def _failed_column(column: str, exc: BaseException | None = None, *, not_applied: bool = False) -> dict[str, Any]:
    if not_applied:
        return {
            "column": column,
            "status": "failed",
            "error": _COLUMN_DDL_NOT_APPLIED,
            "error_type": "NotApplied",
        }
    return {
        "column": column,
        "status": "failed",
        "error": _COLUMN_DDL_FAILED,
        "error_type": type(exc).__name__ if exc is not None else "Error",
    }


def _applied_column(column: str, *, already_present: bool = False) -> dict[str, Any]:
    outcome = {"column": column, "status": "applied"}
    if already_present:
        outcome["already_present"] = True
    return outcome


def _public_columns(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for item in outcomes:
        column_result = {"column": item["column"], "status": item["status"]}
        if item.get("already_present"):
            column_result["already_present"] = True
        if item["status"] == "failed":
            column_result["error"] = item.get("error") or _COLUMN_DDL_FAILED
            column_result["error_type"] = item.get("error_type") or "Error"
        public.append(column_result)
    return public


def _newly_added_columns(outcomes: list[dict[str, Any]]) -> list[str]:
    return [
        item["column"]
        for item in outcomes
        if item["status"] == "applied" and not item.get("already_present")
    ]


def _autofix_batch_status(outcomes: list[dict[str, Any]], *, committed: bool) -> str:
    applied = any(item["status"] == "applied" for item in outcomes)
    failed = any(item["status"] == "failed" for item in outcomes)
    if applied and failed:
        return "partial"
    if failed or not applied:
        return "failed"
    # Reachable when commit() raises after every column DDL succeeded (those stay applied).
    if not committed:
        return "partial"
    return "success"


def _autofix_batch_message(columns: list[dict[str, Any]]) -> str:
    applied = [item["column"] for item in columns if item["status"] == "applied"]
    failed = [item["column"] for item in columns if item["status"] == "failed"]
    parts: list[str] = []
    if applied:
        parts.append("Applied " + ", ".join(applied) + ".")
    if failed:
        parts.append("Failed " + ", ".join(failed) + ".")
    return " ".join(parts) or _COLUMN_DDL_FAILED


def _add_columns(
    owner: str,
    table: str,
    declarations: list[tuple[str, str]],
    deps: AutofixDependencies,
) -> tuple[list[dict[str, Any]], bool]:
    """Apply one ALTER per column and keep every column outcome.

    Oracle DDL commits implicitly, so a later statement failure must not forget
    columns that were already altered. Further DDL stops at the first real error.
    """
    connection = cursor = None
    outcomes: list[dict[str, Any]] = []
    pending = list(declarations)
    try:
        connection = deps.get_connection()
        cursor = connection.cursor()
        while pending:
            column, declaration = pending.pop(0)
            try:
                cursor.execute(f"ALTER TABLE {owner}.{table} ADD {declaration}")
            except oracledb.Error as exc:
                if "ORA-01430" in str(exc):
                    outcomes.append(_applied_column(column, already_present=True))
                    continue
                outcomes.append(_failed_column(column, exc))
                for rest_column, _rest_declaration in pending:
                    outcomes.append(_failed_column(rest_column, not_applied=True))
                pending.clear()
                break
            outcomes.append(_applied_column(column))
        if any(item["status"] == "failed" for item in outcomes):
            _rollback_autofix(connection)
            return outcomes, False
        connection.commit()
        return outcomes, True
    except Exception as exc:
        _rollback_autofix(connection)
        recorded = {item["column"] for item in outcomes}
        for column, _declaration in declarations:
            if column not in recorded:
                outcomes.append(_failed_column(column, exc))
        return outcomes, False
    finally:
        deps.close_resource(cursor, "autofix cursor")
        deps.close_resource(connection, "autofix connection")


def autofix_missing_column(
    owner: str,
    table: str,
    column: str,
    deps: AutofixDependencies,
) -> dict[str, Any]:
    source = _target_source(table)
    if not source:
        return {"error": "Only ODS_<SOURCE>_<TABLE> targets can be fixed automatically."}
    clean_table, source_schema, source_table = source
    source_columns = deps.source_schema_columns(source_schema, source_table)
    column_name = str(column or "").strip().upper()
    if column_name not in source_columns:
        return {"error": "The requested column is not present in the configured source table."}
    declaration = _column_declaration(column_name, source_columns[column_name], deps.map_sql_to_oracle_type)
    outcomes, committed = _add_columns(owner, clean_table, [(column_name, declaration)], deps)
    failed = [item for item in outcomes if item["status"] == "failed"]
    if failed or not committed:
        error_type = failed[0]["error_type"] if failed else "Error"
        return {"error": "The target column could not be added.", "error_type": error_type}
    added = _newly_added_columns(outcomes)
    sync_result = deps.sync_mapping(owner, clean_table)
    if sync_result.get("error"):
        return {
            "error": "The column was added, but ODI mapping synchronization failed.",
            "error_type": sync_result.get("error_type", "ReconciliationError"),
        }
    return {
        "ok": True,
        "message": f"Column {column_name} was added and the ODI mapping was synchronized.",
        "added_columns": added,
        "odi_status": sync_result.get("odi_status"),
    }


def autofix_all_missing_columns(
    owner: str,
    table: str,
    columns: list[str],
    deps: AutofixDependencies,
) -> dict[str, Any]:
    if not columns:
        return {"error": "At least one column is required."}
    source = _target_source(table)
    if not source:
        return {"error": "Only ODS_<SOURCE>_<TABLE> targets can be fixed automatically."}
    clean_table, source_schema, source_table = source
    source_columns = deps.source_schema_columns(source_schema, source_table)
    declarations = [
        (name, _column_declaration(name, source_columns[name], deps.map_sql_to_oracle_type))
        for name in (str(column or "").strip().upper() for column in columns)
        if name in source_columns
    ]
    if not declarations:
        return {"error": "None of the requested columns exist in the configured source table."}
    outcomes, committed = _add_columns(owner, clean_table, declarations, deps)
    columns = _public_columns(outcomes)
    added = _newly_added_columns(outcomes)
    status = _autofix_batch_status(outcomes, committed=committed)
    if status != "success":
        return {
            "status": status,
            "columns": columns,
            "added_columns": added,
            "error": _autofix_batch_message(columns),
        }
    result = deps.sync_mapping(owner, clean_table)
    if result.get("error") or not result.get("ok"):
        applied_names = [item["column"] for item in columns if item["status"] == "applied"]
        message = "ODI mapping synchronization failed."
        if applied_names:
            message = "Applied " + ", ".join(applied_names) + ". " + message
        error_type = result.get("error_type") if isinstance(result.get("error_type"), str) else "ReconciliationError"
        return {
            "status": "partial" if applied_names else "failed",
            "columns": columns,
            "added_columns": added,
            "error": message,
            "error_type": error_type,
            "sync_failed": True,
        }
    merged = dict(result)
    merged["status"] = "success"
    merged["columns"] = columns
    merged["added_columns"] = added
    return merged


def sync_odi_mapping(owner: str, table: str) -> dict[str, Any]:
    owner = str(owner or "").strip().upper()
    clean_table = str(table or "").strip().upper().split(".")[-1]
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", owner):
        return {"error": "Invalid owner or table identifier."}
    source = _target_source(clean_table)
    if not source:
        return {"error": "Only ODS_<SOURCE>_<TABLE> targets can be synchronized."}
    _, source_schema, source_table = source
    mapping_name = f"MAP_{source_schema}_{source_table}_TO_ODS"
    params = {
        "mapping_name": mapping_name,
        "src_model": f"ORACLE_{source_schema}",
        "src_table": f"{source_schema}.{source_table}",
        "tgt_model": "ORACLE_ODS",
        "tgt_table": clean_table,
        "tgt_owner": owner,
        "needs_skey": False,
    }
    from mapping_generator import MappingGenerator

    try:
        with MappingGenerator() as generator:
            generator._register_odi_datastores(params)
            odi_result = generator._create_odi_mapping(params)
    except Exception as exc:
        return {"error": "ODI mapping synchronization failed.", "error_type": type(exc).__name__}
    return {
        "ok": True,
        "message": f"Table metadata and ODI mapping {mapping_name} were synchronized.",
        "odi_status": odi_result,
    }
