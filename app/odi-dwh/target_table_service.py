from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import oracledb
import pandas as pd

from exceptions import DWHBaseError, MappingGenerationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetTableContext:
    owner: str
    table: str
    drop_if_exists: bool
    ddl_user: str


_MAX_CONSTRAINT_NAME = 30
_KEY_KINDS = {"unique": "UK", "primary": "PK"}


def key_constraint_name(table: str, columns: list[str], kind: str) -> str:
    """Deterministic constraint name (<= 30 chars): UK_<T>_<COL> or PK_<T>."""
    prefix = _KEY_KINDS[kind]
    table_name = str(table).upper()
    cols = [str(column).upper() for column in columns]
    if kind == "primary":
        name = f"{prefix}_{table_name}"
    else:
        name = f"{prefix}_{table_name}_{cols[0]}"
    if len(name) <= _MAX_CONSTRAINT_NAME and (kind == "primary" or len(cols) == 1):
        return name
    digest = hashlib.sha1(f"{table_name}|{','.join(cols)}".encode("utf-8")).hexdigest()[:6].upper()
    head = name[: _MAX_CONSTRAINT_NAME - len(digest) - 1].rstrip("_")
    return f"{head}_{digest}"


def key_constraint_sql(table: str, columns: list[str], kind: str) -> str:
    """Inline CREATE TABLE / ALTER TABLE constraint clause for the target key."""
    quoted = ", ".join(f'"{str(column).upper()}"' for column in columns)
    keyword = "PRIMARY KEY" if kind == "primary" else "UNIQUE"
    return f"    CONSTRAINT {key_constraint_name(table, columns, kind)} {keyword} ({quoted})"


def _plan_key(plan: dict[str, Any]) -> tuple[str, list[str]] | None:
    if plan.get("primary_key"):
        return "primary", list(plan["primary_key"])
    if plan.get("unique_key"):
        return "unique", list(plan["unique_key"])
    return None


def _close_resource(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except (AttributeError, oracledb.Error) as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def _connection():
    from db import get_connection

    return get_connection(use_ddl=True)


def _target_exists(context: TargetTableContext) -> bool:
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:o AND TABLE_NAME=:t",
            {"o": context.owner, "t": context.table},
        )
        return cursor.fetchone()[0] > 0
    except Exception as exc:
        raise MappingGenerationError("Target table existence check failed") from exc
    finally:
        _close_resource(cursor, "target existence cursor")
        _close_resource(connection, "target existence connection")


def _drop_target(context: TargetTableContext) -> None:
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(f"DROP TABLE {context.owner}.{context.table} CASCADE CONSTRAINTS")
        connection.commit()
    except Exception as exc:
        raise MappingGenerationError("Target table drop failed") from exc
    finally:
        _close_resource(cursor, "target drop cursor")
        _close_resource(connection, "target drop connection")


def _target_columns(context: TargetTableContext) -> set[str]:
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :tname",
            {"owner": context.owner, "tname": context.table},
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}
    except Exception as exc:
        raise MappingGenerationError("Target table column read failed") from exc
    finally:
        _close_resource(cursor, "target column cursor")
        _close_resource(connection, "target column connection")


def read_table_schema(owner: str, table: str) -> pd.DataFrame:
    """Read table columns through the target (DDL) connection with an explicit owner.

    Same column contract as OracleSourceConnector.get_table_schema, so internal
    ODS_/DWH_ sources do not depend on the source connector's default schema.
    """
    from source_connector import normalize_source_identifier

    owner_name = normalize_source_identifier(owner)
    table_name = normalize_source_identifier(table)
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH,
                   c.DATA_PRECISION, c.DATA_SCALE, c.NULLABLE, cm.COMMENTS
            FROM ALL_TAB_COLUMNS c
            LEFT JOIN ALL_COL_COMMENTS cm
              ON c.OWNER = cm.OWNER AND c.TABLE_NAME = cm.TABLE_NAME
             AND c.COLUMN_NAME = cm.COLUMN_NAME
            WHERE c.OWNER = :o AND c.TABLE_NAME = :t
            ORDER BY c.COLUMN_ID
            """,
            {"o": owner_name, "t": table_name},
        )
        rows = cursor.fetchall()
    except (oracledb.Error, DWHBaseError) as exc:
        raise MappingGenerationError("Internal source column read failed") from exc
    finally:
        _close_resource(cursor, "internal source cursor")
        _close_resource(connection, "internal source connection")

    frame = pd.DataFrame(
        rows,
        columns=[
            "COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION",
            "DATA_SCALE", "NULLABLE", "COMMENTS",
        ],
    )
    full_types = []
    for _, row in frame.iterrows():
        base = str(row["DATA_TYPE"])
        precision, scale, length = row["DATA_PRECISION"], row["DATA_SCALE"], row["DATA_LENGTH"]
        if base in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"):
            full_types.append(f"{base}({int(length)})")
        elif base == "NUMBER" and pd.notna(precision) and pd.notna(scale) and int(scale) > 0:
            full_types.append(f"NUMBER({int(precision)},{int(scale)})")
        elif base == "NUMBER" and pd.notna(precision):
            full_types.append(f"NUMBER({int(precision)})")
        else:
            full_types.append(base)
    frame["FULL_DATA_TYPE"] = full_types
    return frame[[
        "COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION",
        "DATA_SCALE", "NULLABLE", "FULL_DATA_TYPE", "COMMENTS",
    ]]


def _existing_key_column_sets(context: TargetTableContext) -> list[tuple[str, ...]]:
    """Column sets of existing primary/unique constraints on the target table."""
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT c.CONSTRAINT_NAME, cc.COLUMN_NAME
            FROM ALL_CONSTRAINTS c
            JOIN ALL_CONS_COLUMNS cc
              ON c.OWNER = cc.OWNER AND c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
            WHERE c.OWNER = :o AND c.TABLE_NAME = :t AND c.CONSTRAINT_TYPE IN ('P', 'U')
            ORDER BY c.CONSTRAINT_NAME, cc.POSITION
            """,
            {"o": context.owner, "t": context.table},
        )
        grouped: dict[str, list[str]] = {}
        for name, column in cursor.fetchall():
            grouped.setdefault(str(name), []).append(str(column).upper())
        return [tuple(columns) for columns in grouped.values()]
    except (oracledb.Error, DWHBaseError) as exc:
        raise MappingGenerationError("Target key lookup failed") from exc
    finally:
        _close_resource(cursor, "target key cursor")
        _close_resource(connection, "target key connection")


def _add_key_constraint(context: TargetTableContext, constraint_sql: str) -> None:
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(f"ALTER TABLE {context.owner}.{context.table} ADD {constraint_sql.strip()}")
        connection.commit()
    except (oracledb.Error, DWHBaseError) as exc:
        raise MappingGenerationError("Target key creation failed") from exc
    finally:
        _close_resource(cursor, "target key add cursor")
        _close_resource(connection, "target key add connection")


def _ensure_key_on_existing(context: TargetTableContext, plan: dict[str, Any]) -> dict[str, Any]:
    """Absent-only: add the requested key to an existing table when it is missing."""
    requested = _plan_key(plan)
    if requested is None:
        return {"status": "NONE", "columns": []}
    kind, columns = requested
    constraint = key_constraint_name(context.table, columns, kind)
    wanted = set(columns)
    if any(set(existing) == wanted for existing in _existing_key_column_sets(context)):
        return {"status": "ALREADY_PRESENT", "columns": columns, "constraint": constraint}
    _add_key_constraint(context, key_constraint_sql(context.table, columns, kind))
    return {"status": "CREATED", "columns": columns, "constraint": constraint}


def _reconcile_existing(
    context: TargetTableContext,
    desired_columns: list[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    existing = _target_columns(context)
    missing = [(name, type_sql) for name, type_sql in desired_columns if name not in existing]
    if not missing:
        return [], []
    connection = cursor = None
    alter_sqls: list[str] = []
    try:
        connection = _connection()
        cursor = connection.cursor()
        for name, type_sql in missing:
            sql = f'ALTER TABLE {context.owner}.{context.table} ADD "{name}" {type_sql}'
            cursor.execute(sql)
            alter_sqls.append(sql)
        connection.commit()
        return [name for name, _ in missing], alter_sqls
    except Exception as exc:
        raise MappingGenerationError("Target table reconciliation failed") from exc
    finally:
        _close_resource(cursor, "target reconciliation cursor")
        _close_resource(connection, "target reconciliation connection")


def _create_target(context: TargetTableContext, column_definitions: list[str]) -> str:
    ddl = f"CREATE TABLE {context.owner}.{context.table} (\n" + ",\n".join(column_definitions) + "\n)"
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        cursor.execute(ddl)
        connection.commit()
    except oracledb.DatabaseError as exc:
        if "ORA-00955" not in str(exc):
            raise MappingGenerationError("Target table creation failed") from exc
    finally:
        _close_resource(cursor, "target create cursor")
        _close_resource(connection, "target create connection")
    return ddl


def _apply_grants(context: TargetTableContext) -> None:
    statements = []
    if context.ddl_user != "APP_USER":
        statements.append(f"GRANT SELECT ON {context.owner}.{context.table} TO APP_USER")
    statements.extend(
        f"GRANT {privilege} ON {context.owner}.{context.table} TO ODI_EXEC"
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    )
    connection = cursor = None
    try:
        connection = _connection()
        cursor = connection.cursor()
        for statement in statements:
            try:
                cursor.execute(statement)
            except oracledb.DatabaseError as exc:
                logger.debug("Target grant skipped: %s", type(exc).__name__)
        connection.commit()
    except Exception as exc:
        logger.warning("Target grants could not be completed: %s", type(exc).__name__)
    finally:
        _close_resource(cursor, "target grant cursor")
        _close_resource(connection, "target grant connection")


def _write_creation_audit(generator: Any, context: TargetTableContext, ddl: str) -> None:
    from skey_manager import SkeyManager

    try:
        with SkeyManager() as manager:
            manager._log_operation(
                conn=manager._get_conn(),
                batch_id=generator._batch_id,
                operation="CREATE_TABLE",
                object_owner=context.owner,
                object_name=context.table,
                ddl_text=ddl,
                status="SUCCESS",
            )
    except Exception as exc:
        logger.warning("Target creation audit skipped: %s", type(exc).__name__)


def execute_target_table_plan(
    generator: Any,
    params: dict[str, Any],
    plan: dict[str, Any],
    ddl_user: str,
) -> dict[str, Any]:
    context = TargetTableContext(
        owner=params["tgt_owner"].upper(),
        table=params["tgt_table"].upper(),
        drop_if_exists=params.get("drop_target_if_exists") is True,
        ddl_user=(ddl_user or "APP_USER").upper(),
    )
    exists = _target_exists(context)
    if exists and context.drop_if_exists:
        _drop_target(context)
        generator._add_log("create_target_table", "INFO", f"Target table dropped: {context.owner}.{context.table}")
        exists = False
    if exists:
        added, alter_sqls = _reconcile_existing(context, plan["desired_col_defs"])
        status = "RECONCILED" if added else "ALREADY_IN_SYNC"
        key_result = _ensure_key_on_existing(context, plan)
        generator._add_log("create_target_table", "SUCCESS", f"Target table reused; {len(added)} columns added")
        return {
            "status": status,
            "key": key_result,
            "table": f"{context.owner}.{context.table}",
            "columns_count": len(plan["src_columns"]),
            "default_columns_added": plan["defaults_added"],
            "added_columns": added,
            "ddl": "\n".join(alter_sqls),
        }
    requested_key = _plan_key(plan)
    column_definitions = list(plan["col_defs"])
    key_result: dict[str, Any] = {"status": "NONE", "columns": []}
    if requested_key is not None:
        kind, key_columns = requested_key
        column_definitions.append(key_constraint_sql(context.table, key_columns, kind))
        key_result = {
            "status": "CREATED",
            "columns": key_columns,
            "constraint": key_constraint_name(context.table, key_columns, kind),
        }
    ddl = _create_target(context, column_definitions)
    _apply_grants(context)
    _write_creation_audit(generator, context, ddl)
    generator._add_log(
        "create_target_table",
        "SUCCESS",
        f"{context.owner}.{context.table} created with {len(plan['src_columns'])} source columns",
    )
    return {
        "status": "CREATED",
        "key": key_result,
        "table": f"{context.owner}.{context.table}",
        "ddl": ddl,
        "columns_count": len(plan["src_columns"]),
        "default_columns_added": plan["defaults_added"],
    }
