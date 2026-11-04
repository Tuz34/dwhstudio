from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import oracledb

from exceptions import MappingGenerationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetTableContext:
    owner: str
    table: str
    drop_if_exists: bool
    ddl_user: str


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
        generator._add_log("create_target_table", "SUCCESS", f"Target table reused; {len(added)} columns added")
        return {
            "status": status,
            "table": f"{context.owner}.{context.table}",
            "columns_count": len(plan["src_columns"]),
            "default_columns_added": plan["defaults_added"],
            "added_columns": added,
            "ddl": "\n".join(alter_sqls),
        }
    ddl = _create_target(context, plan["col_defs"])
    _apply_grants(context)
    _write_creation_audit(generator, context, ddl)
    generator._add_log(
        "create_target_table",
        "SUCCESS",
        f"{context.owner}.{context.table} created with {len(plan['src_columns'])} source columns",
    )
    return {
        "status": "CREATED",
        "table": f"{context.owner}.{context.table}",
        "ddl": ddl,
        "columns_count": len(plan["src_columns"]),
        "default_columns_added": plan["defaults_added"],
    }
