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


def _add_columns(
    owner: str,
    table: str,
    declarations: list[tuple[str, str]],
    deps: AutofixDependencies,
) -> tuple[list[str], str | None]:
    connection = cursor = None
    added: list[str] = []
    try:
        connection = deps.get_connection()
        cursor = connection.cursor()
        for column, declaration in declarations:
            try:
                cursor.execute(f"ALTER TABLE {owner}.{table} ADD {declaration}")
                added.append(column)
            except oracledb.Error as exc:
                if "ORA-01430" not in str(exc):
                    raise
        connection.commit()
        return added, None
    except Exception as exc:
        if connection is not None:
            try:
                connection.rollback()
            except (AttributeError, oracledb.Error) as rollback_exc:
                logger.warning("Autofix rollback failed: %s", type(rollback_exc).__name__)
        return [], type(exc).__name__
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
    added, error_type = _add_columns(owner, clean_table, [(column_name, declaration)], deps)
    if error_type:
        return {"error": "The target column could not be added.", "error_type": error_type}
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
    added, error_type = _add_columns(owner, clean_table, declarations, deps)
    if error_type:
        return {"error": "The target columns could not be added.", "error_type": error_type}
    result = deps.sync_mapping(owner, clean_table)
    if result.get("ok"):
        result["added_columns"] = added
    return result


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
