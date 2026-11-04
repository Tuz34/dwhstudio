from __future__ import annotations

import re
import sys

import pandas as pd

from db import execute_dml, execute_select, get_table_schema
from lineage_manager import LineageManager

_ALLOWED_OWNERS = {"DWH", "HR", "SH"}
_SCHEMA_OBJECT_RE = re.compile(
    r"^(?P<owner>[A-Z][A-Z0-9_]*)\.(?P<table>[A-Z][A-Z0-9_]*)(?:\.(?P<column>[A-Z][A-Z0-9_]*))?$"
)
_VARCHAR_TYPE_RE = re.compile(r"^VARCHAR2?\((?P<length>\d+)\)$", re.IGNORECASE)


def _validate_owner(owner: str) -> bool:
    return owner.upper() in _ALLOWED_OWNERS


def _app_dependency(name: str, fallback):
    app_module = sys.modules.get("app")
    return getattr(app_module, name, fallback) if app_module is not None else fallback


def _helper(name: str, fallback):
    return _app_dependency(name, fallback)


def _execute_select(sql: str):
    return _app_dependency("execute_select", execute_select)(sql)


def _execute_dml(sql: str):
    return _app_dependency("execute_dml", execute_dml)(sql)


def _get_table_schema(table_ref: str):
    return _app_dependency("get_table_schema", get_table_schema)(table_ref)


def _lineage_manager_cls():
    return _app_dependency("LineageManager", LineageManager)

def _parse_schema_object_ref(object_ref: str) -> dict:
    clean = (object_ref or "").strip().upper()
    match = _SCHEMA_OBJECT_RE.match(clean)
    if not match:
        raise ValueError("object must be OWNER.TABLE or OWNER.TABLE.COLUMN")
    return {
        "owner": match.group("owner"),
        "table": match.group("table"),
        "column": match.group("column"),
    }


def _normalize_column_row(row: dict) -> dict:
    def pick(*names, default=None):
        for name in names:
            if name in row:
                return row[name]
            upper = name.upper()
            if upper in row:
                return row[upper]
        return default

    data_type = str(pick("data_type", "DATA_TYPE", default="") or "").upper()
    data_length = pick("data_length", "DATA_LENGTH")
    precision = pick("data_precision", "DATA_PRECISION")
    scale = pick("data_scale", "DATA_SCALE")
    full = pick("full_data_type", "FULL_DATA_TYPE")
    if not full:
        if data_type in {"VARCHAR2", "VARCHAR", "CHAR", "NCHAR", "NVARCHAR2"} and data_length:
            full = f"{data_type}({int(data_length)})"
        elif data_type == "NUMBER" and precision is not None:
            full = f"NUMBER({precision},{scale})" if scale is not None else f"NUMBER({precision})"
        else:
            full = data_type

    return {
        "column_name": str(pick("column_name", "COLUMN_NAME") or "").upper(),
        "data_type": data_type,
        "data_length": int(data_length) if data_length is not None and not pd.isna(data_length) else None,
        "data_precision": int(precision) if precision is not None and not pd.isna(precision) else None,
        "data_scale": int(scale) if scale is not None and not pd.isna(scale) else None,
        "nullable": str(pick("nullable", "NULLABLE", default="") or ""),
        "full_data_type": str(full or "").upper(),
    }


def _get_db_column_definitions(owner: str, table: str) -> list[dict]:
    owner = owner.upper()
    table = table.upper()
    if not _validate_owner(owner) or not re.match(r"^[A-Z][A-Z0-9_]*$", table):
        raise ValueError("Invalid owner or table name")
    df = _execute_select(
        f"""
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
               DATA_PRECISION, DATA_SCALE, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER = '{owner}' AND TABLE_NAME = '{table}'
        ORDER BY COLUMN_ID
        """
    )
    return [_normalize_column_row(row) for row in df.to_dict(orient="records")]


def _max_text_length(owner: str, table: str, column: str) -> int | None:
    # Identifiers are validated by _parse_schema_object_ref before this helper is called.
    df = _execute_select(
        f'SELECT MAX(LENGTH("{column.upper()}")) AS MAX_LEN FROM "{owner.upper()}"."{table.upper()}"'
    )
    if df.empty:
        return None
    value = df.iloc[0].get("MAX_LEN")
    return None if value is None or pd.isna(value) else int(value)


def _parse_varchar_length(type_name: str) -> int | None:
    match = _VARCHAR_TYPE_RE.match((type_name or "").strip().upper())
    return int(match.group("length")) if match else None


def _classify_type_change(current_col: dict, new_type: str, owner: str, table: str) -> dict:
    current_full = str(current_col.get("full_data_type") or "").upper()
    new_full = (new_type or "").strip().upper()
    current_len = _parse_varchar_length(current_full)
    new_len = _parse_varchar_length(new_full)
    if current_len is not None and new_len is not None:
        if new_len > current_len:
            return {"classification": "safe_widening", "reason": "New VARCHAR length is larger than current length."}
        if new_len == current_len:
            return {"classification": "no_op", "reason": "Column already has the requested VARCHAR length."}
        max_len = _helper("_max_text_length", _max_text_length)(owner, table, current_col["column_name"])
        if max_len is not None and max_len > new_len:
            return {
                "classification": "blocked_narrowing",
                "reason": "Existing data length exceeds requested VARCHAR length.",
                "max_data_length": max_len,
            }
        return {
            "classification": "narrowing_requires_override",
            "reason": "Requested VARCHAR length is smaller; explicit override is required even if current data fits.",
            "max_data_length": max_len,
        }
    if current_full == new_full:
        return {"classification": "no_op", "reason": "Column already has the requested type."}
    return {"classification": "type_change_requires_review", "reason": "Non-VARCHAR type change requires manual review."}


def _schema_impact_payload(object_ref: str) -> dict:
    obj = _parse_schema_object_ref(object_ref)
    if not _validate_owner(obj["owner"]):
        raise ValueError(f"Invalid owner: {obj['owner']}")

    table_ref = f"{obj['owner']}.{obj['table']}"
    columns = []
    try:
        df = _get_table_schema(table_ref)
        rows = df.to_dict(orient="records")
        for row in rows:
            col_name = str(row.get("COLUMN_NAME") or row.get("column_name") or "").upper()
            if obj["column"] and col_name != obj["column"]:
                continue
            columns.append({
                "column_name": col_name,
                "data_type": str(row.get("FULL_DATA_TYPE") or row.get("DATA_TYPE") or row.get("data_type") or ""),
                "nullable": str(row.get("NULLABLE") or row.get("nullable") or ""),
            })
    except Exception:
        columns = []

    with _lineage_manager_cls()() as lm:
        mappings = lm.get_impact_analysis(obj["table"], direction="both")

    downstream_tables = sorted({
        str(item.get("related_table") or "")
        for item in mappings
        if str(item.get("direction") or "").lower() == "downstream" and item.get("related_table")
    })
    return {
        "object": obj,
        "read_only": True,
        "mappings": mappings,
        "downstream_tables": downstream_tables,
        "columns": columns,
        "affected_count": len(mappings),
    }


def _schema_change_preflight_payload(body: dict) -> dict:
    owner = str(body.get("owner") or "").strip().upper()
    table = str(body.get("table") or "").strip().upper()
    column = str(body.get("column") or "").strip().upper()
    operation = str(body.get("op") or "").strip().lower()
    if not owner or not table:
        raise ValueError("owner and table are required")
    if not _validate_owner(owner):
        raise ValueError(f"Invalid owner: {owner}")
    if not re.match(r"^[A-Z][A-Z0-9_]*$", table):
        raise ValueError("Invalid table name")
    if column and not re.match(r"^[A-Z][A-Z0-9_]*$", column):
        raise ValueError("Invalid column name")

    columns = _helper("_get_db_column_definitions", _get_db_column_definitions)(owner, table)
    current_col = next((col for col in columns if col["column_name"] == column), None) if column else None

    risk = {"classification": "review_required", "reason": "Operation requires manual review."}
    if operation == "modify_type":
        if not column:
            raise ValueError("column is required for modify_type")
        if not current_col:
            raise ValueError(f"Column not found: {owner}.{table}.{column}")
        new_type = str(body.get("new_type") or "").strip().upper()
        if not new_type:
            raise ValueError("new_type is required for modify_type")
        risk = _helper("_classify_type_change", _classify_type_change)(current_col, new_type, owner, table)
    elif operation == "rename_column":
        new_name = str(body.get("new_name") or "").strip().upper()
        if not column:
            raise ValueError("column is required for rename_column")
        if not new_name:
            raise ValueError("new_name is required for rename operations")
        if not re.match(r"^[A-Z][A-Z0-9_]*$", new_name):
            raise ValueError("Invalid new_name")
        requested_col = next((col for col in columns if col["column_name"] == new_name), None)
        if not current_col and requested_col:
            risk = {"classification": "no_op", "reason": "Column already has the requested name."}
        elif not current_col:
            raise ValueError(f"Column not found: {owner}.{table}.{column}")
        elif requested_col:
            raise ValueError(f"Target column already exists: {owner}.{table}.{new_name}")
    elif operation == "rename_table":
        new_name = str(body.get("new_name") or "").strip().upper()
        if not new_name:
            raise ValueError("new_name is required for rename operations")
        if not re.match(r"^[A-Z][A-Z0-9_]*$", new_name):
            raise ValueError("Invalid new_name")
        new_table_columns = _helper("_get_db_column_definitions", _get_db_column_definitions)(owner, new_name)
        if not columns and new_table_columns:
            risk = {"classification": "no_op", "reason": "Table already has the requested name."}
        elif not columns:
            raise ValueError(f"Table not found: {owner}.{table}")
        elif new_table_columns:
            raise ValueError(f"Target table already exists: {owner}.{new_name}")
    else:
        raise ValueError("Unsupported operation")

    impact_ref = f"{owner}.{table}.{column}" if column else f"{owner}.{table}"
    impact = _helper("_schema_impact_payload", _schema_impact_payload)(impact_ref)
    blocked = risk["classification"] == "blocked_narrowing"
    next_step = (
        "No change is needed; the requested rename already appears to be applied."
        if risk["classification"] == "no_op" and operation in {"rename_column", "rename_table"}
        else "Review this preflight output and approve a separate apply call. No change was applied."
    )
    return {
        "read_only": True,
        "operation": operation,
        "object": {"owner": owner, "table": table, "column": column or None},
        "current_column": current_col,
        "requested": {
            "new_type": str(body.get("new_type") or "").strip().upper() or None,
            "new_name": str(body.get("new_name") or "").strip().upper() or None,
        },
        "risk": risk,
        "impact": impact,
        "requires_approval": True,
        "allowed_to_apply": not blocked,
        "next_step": next_step,
    }


def _quote_ident(identifier: str) -> str:
    clean = (identifier or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9_]*$", clean):
        raise ValueError(f"Invalid identifier: {identifier}")
    return f'"{clean}"'


def _normalize_sql_type(type_name: str) -> str:
    clean = (type_name or "").strip().upper()
    if not re.match(r"^[A-Z0-9_]+(?:\(\d+(?:,\d+)?\))?$", clean):
        raise ValueError("Invalid SQL type")
    return clean


def _parse_sql_type_parts(type_name: str) -> tuple[str, int | None, int | None]:
    clean = _normalize_sql_type(type_name)
    match = re.match(r"^(?P<name>[A-Z0-9_]+)(?:\((?P<first>\d+)(?:,(?P<second>\d+))?\))?$", clean)
    if not match:
        raise ValueError("Invalid SQL type")
    length = int(match.group("first")) if match.group("first") else None
    scale = int(match.group("second")) if match.group("second") else None
    return match.group("name"), length, scale


def _get_object_grants(owner: str, table: str) -> list[dict]:
    owner = owner.upper()
    table = table.upper()
    if not _validate_owner(owner) or not re.match(r"^[A-Z][A-Z0-9_]*$", table):
        raise ValueError("Invalid grant object")
    df = _execute_select(
        f"""
        SELECT GRANTEE, PRIVILEGE
        FROM ALL_TAB_PRIVS
        WHERE TABLE_SCHEMA = '{owner}' AND TABLE_NAME = '{table}'
        ORDER BY GRANTEE, PRIVILEGE
        """
    )
    grants = []
    for row in df.to_dict(orient="records"):
        grantee = str(row.get("GRANTEE") or "").upper()
        privilege = str(row.get("PRIVILEGE") or "").upper()
        if re.match(r"^[A-Z][A-Z0-9_]*$", grantee) and re.match(r"^[A-Z][A-Z0-9_ ]*$", privilege):
            grants.append({"grantee": grantee, "privilege": privilege})
    return grants


def _column_after(owner: str, table: str, column: str | None) -> dict | None:
    if not column:
        return None
    cols = _helper("_get_db_column_definitions", _get_db_column_definitions)(owner, table)
    return next((col for col in cols if col["column_name"] == column.upper()), None)


def _schema_change_apply_db_payload(body: dict) -> dict:
    if body.get("approved") is not True:
        raise ValueError("approved:true is required for DB apply.")

    preflight = _helper("_schema_change_preflight_payload", _schema_change_preflight_payload)(body)
    if not preflight.get("allowed_to_apply"):
        return {
            "applied": False,
            "status": "BLOCKED",
            "preflight": preflight,
            "error": "Preflight blocked this change.",
        }

    obj = preflight["object"]
    owner = obj["owner"]
    table = obj["table"]
    column = obj.get("column")
    operation = preflight["operation"]
    requested = preflight.get("requested") or {}
    risk = preflight.get("risk") or {}
    executed_sql = []

    if risk.get("classification") == "no_op":
        return {
            "applied": False,
            "status": "NO_OP",
            "operation": operation,
            "preflight": preflight,
        }

    if operation == "modify_type":
        new_type = _helper("_normalize_sql_type", _normalize_sql_type)(requested.get("new_type") or "")
        sql = (
            f"ALTER TABLE {_helper('_quote_ident', _quote_ident)(owner)}.{_helper('_quote_ident', _quote_ident)(table)} "
            f"MODIFY ({_helper('_quote_ident', _quote_ident)(column)} {new_type})"
        )
        _execute_dml(sql)
        executed_sql.append(sql)
        after = _helper("_column_after", _column_after)(owner, table, column)

    elif operation == "rename_column":
        new_name = str(requested.get("new_name") or "").strip().upper()
        sql = (
            f"ALTER TABLE {_helper('_quote_ident', _quote_ident)(owner)}.{_helper('_quote_ident', _quote_ident)(table)} "
            f"RENAME COLUMN {_helper('_quote_ident', _quote_ident)(column)} TO {_helper('_quote_ident', _quote_ident)(new_name)}"
        )
        _execute_dml(sql)
        executed_sql.append(sql)
        after = _helper("_column_after", _column_after)(owner, table, new_name)

    elif operation == "rename_table":
        new_name = str(requested.get("new_name") or "").strip().upper()
        grants = _helper("_get_object_grants", _get_object_grants)(owner, table)
        sql = f"ALTER TABLE {_helper('_quote_ident', _quote_ident)(owner)}.{_helper('_quote_ident', _quote_ident)(table)} RENAME TO {_helper('_quote_ident', _quote_ident)(new_name)}"
        _execute_dml(sql)
        executed_sql.append(sql)
        for grant in grants:
            grant_sql = (
                f"GRANT {grant['privilege']} ON {_helper('_quote_ident', _quote_ident)(owner)}.{_helper('_quote_ident', _quote_ident)(new_name)} "
                f"TO {_helper('_quote_ident', _quote_ident)(grant['grantee'])}"
            )
            _execute_dml(grant_sql)
            executed_sql.append(grant_sql)
        after = None

    else:
        raise ValueError("Unsupported operation")

    return {
        "applied": True,
        "status": "APPLIED",
        "operation": operation,
        "preflight": preflight,
        "executed_sql": executed_sql,
        "after": after,
        "next_step": "Run ODI datastore sync after reviewing DB read-back.",
    }


def _schema_change_sync_odi_payload(body: dict) -> dict:
    if body.get("approved") is not True:
        raise ValueError("approved:true is required for ODI metadata sync.")

    model_code = str(body.get("model_code") or "").strip().upper()
    table = str(body.get("table") or "").strip().upper()
    column = str(body.get("column") or "").strip().upper()
    operation = str(body.get("op") or "").strip().lower()
    new_name = str(body.get("new_name") or "").strip().upper() or None
    if not model_code or not re.match(r"^[A-Z][A-Z0-9_]*$", model_code):
        raise ValueError("Invalid model_code")
    if not table or not re.match(r"^[A-Z][A-Z0-9_]*$", table):
        raise ValueError("Invalid table name")
    if column and not re.match(r"^[A-Z][A-Z0-9_]*$", column):
        raise ValueError("Invalid column name")
    if new_name and not re.match(r"^[A-Z][A-Z0-9_]*$", new_name):
        raise ValueError("Invalid new_name")

    column_type = None
    column_length = None
    column_scale = None
    if operation == "modify_type":
        if not column:
            raise ValueError("column is required for modify_type")
        column_type, column_length, column_scale = _parse_sql_type_parts(str(body.get("new_type") or ""))
    elif operation == "rename_column":
        if not column or not new_name:
            raise ValueError("column and new_name are required for rename_column")
    elif operation == "rename_table":
        if not new_name:
            raise ValueError("new_name is required for rename_table")
    else:
        raise ValueError("Unsupported operation")

    import odi_orchestrator

    with odi_orchestrator.groovy_approval_context(body.get("approved")):
        result = odi_orchestrator.OdiOrchestrator().sync_datastore_column(
            model_code=model_code,
            table_name=table,
            operation=operation,
            column_name=column or None,
            column_type=column_type,
            column_length=column_length,
            column_scale=column_scale,
            new_name=new_name,
        )
    failure_reason = odi_orchestrator.groovy_sync_failure_reason(result)
    if failure_reason:
        return {
            "applied": False,
            "status": "ODI_METADATA_SYNC_FAILED",
            "operation": operation,
            "model_code": model_code,
            "table": table,
            "column": column or None,
            "new_name": new_name,
            "reason": failure_reason,
            "next_step": "Review ODI metadata state before retrying the sync.",
        }
    return {
        "applied": True,
        "status": "ODI_METADATA_SYNCED",
        "operation": operation,
        "model_code": model_code,
        "table": table,
        "column": column or None,
        "new_name": new_name,
        "result": result,
        "next_step": "Regenerate affected ODI scenario after reviewing metadata sync output.",
    }


def _schema_change_reconcile_scenario_payload(body: dict) -> dict:
    if body.get("approved") is not True:
        raise ValueError("approved:true is required for scenario reconcile.")

    mapping_name = str(body.get("mapping_name") or "").strip().upper()
    version = str(body.get("scenario_version") or "001").strip() or "001"
    scenario_name = str(body.get("scenario_name") or f"{mapping_name}_SCN").strip().upper()
    project = str(body.get("project") or "").strip().upper() or None
    folder = str(body.get("folder") or "").strip().upper() or None

    if not mapping_name or not re.match(r"^[A-Z][A-Z0-9_]*$", mapping_name):
        raise ValueError("Invalid mapping_name")
    if not scenario_name or not re.match(r"^[A-Z][A-Z0-9_]*$", scenario_name):
        raise ValueError("Invalid scenario_name")
    if not re.match(r"^[A-Za-z0-9_.-]+$", version):
        raise ValueError("Invalid scenario_version")
    if project and not re.match(r"^[A-Z][A-Z0-9_]*$", project):
        raise ValueError("Invalid project")
    if folder and not re.match(r"^[A-Z][A-Z0-9_]*$", folder):
        raise ValueError("Invalid folder")

    import odi_orchestrator

    orch = odi_orchestrator.OdiOrchestrator()
    generation = orch.generate_scenario_metadata(
        approved=True,
        mapping_name=mapping_name,
        scenario_name=scenario_name,
        version=version,
        project=project,
        folder=folder,
    )
    if str(generation.get("status") or "").lower() not in {"success", "passed", "already_ready"}:
        return {
            "applied": False,
            "status": "SCENARIO_RECONCILE_FAILED",
            "mapping_name": mapping_name,
            "scenario_name": scenario_name,
            "scenario_version": version,
            "generation": generation,
            "probe": None,
            "will_run_scenario": False,
            "error": generation.get("error") or "Scenario metadata generation failed.",
        }
    probe = orch.scenario_probe(
        approved=True,
        scenario_name=scenario_name,
        version=version,
    )
    return {
        "applied": True,
        "status": "SCENARIO_RECONCILED",
        "mapping_name": mapping_name,
        "scenario_name": scenario_name,
        "scenario_version": version,
        "generation": generation,
        "probe": probe,
        "will_run_scenario": False,
        "next_step": "Optional run/load remains separate and must use the truncate/append run confirmation.",
    }


# API query helpers

