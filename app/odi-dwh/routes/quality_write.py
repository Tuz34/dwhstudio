from __future__ import annotations

from flask import Blueprint, jsonify, request

from odi_orchestrator import groovy_approval_context
from routes.deps import get_app_module
from routes.error_responses import redacted_write_error


bp = Blueprint("quality_write", __name__)
limiter = get_app_module().limiter


def _owner_from_body(app_module, body):
    raw_owner = body.get("owner", "DWH")
    if not isinstance(raw_owner, str):
        return None, (jsonify({"error": "Invalid owner."}), 400)
    owner = raw_owner.strip().upper()
    if not app_module._validate_owner(owner):
        return None, (jsonify({"error": "Invalid owner."}), 400)
    return owner, None


def _owner_table_from_body(app_module, body):
    owner, error = _owner_from_body(app_module, body)
    if error:
        return None, None, error

    raw_table = body.get("table", "")
    if not isinstance(raw_table, str) or not raw_table.strip():
        return None, None, (jsonify({"error": "Table is required."}), 400)
    table = raw_table.strip().upper()

    if "." in table:
        parts = table.split(".")
        if len(parts) != 2:
            return None, None, (jsonify({"error": "Invalid table name."}), 400)
        qualified_owner, table = parts
        if qualified_owner != owner or not app_module._validate_owner(qualified_owner):
            return None, None, (jsonify({"error": "Invalid table owner."}), 400)

    if not app_module._valid_identifier(table):
        return None, None, (jsonify({"error": "Invalid table name."}), 400)
    return owner, table, None


def _valid_column(app_module, value) -> str | None:
    if not isinstance(value, str):
        return None
    column = value.strip().upper()
    return column if app_module._valid_identifier(column) else None


def _result_response(action: str, result):
    if isinstance(result, dict) and result.get("error"):
        return jsonify({"error": f"{action} failed.", "error_type": "OperationError"}), 500
    return jsonify(result)


_AUTOFIX_BATCH_STATUSES = {"success", "partial", "failed"}
_SAFE_COLUMN_ERRORS = {"Column DDL failed.", "Column DDL was not applied."}


def _safe_error_type(value: object) -> str | None:
    if isinstance(value, str) and value.isidentifier() and len(value) <= 80:
        return value
    return None


def _sanitized_autofix_column(item: dict) -> dict:
    status = item.get("status")
    public = {"column": item.get("column"), "status": status}
    if item.get("already_present") is True:
        public["already_present"] = True
    if status != "failed":
        return public
    error = item.get("error")
    public["error"] = error if error in _SAFE_COLUMN_ERRORS else "Column DDL failed."
    error_type = _safe_error_type(item.get("error_type"))
    if error_type:
        public["error_type"] = error_type
    return public


def _autofix_failure_message(columns: list[dict], *, sync_failed: bool) -> str:
    applied = [
        item["column"]
        for item in columns
        if item.get("status") == "applied" and isinstance(item.get("column"), str)
    ]
    failed = [
        item["column"]
        for item in columns
        if item.get("status") == "failed" and isinstance(item.get("column"), str)
    ]
    parts: list[str] = []
    if applied:
        parts.append("Applied " + ", ".join(applied) + ".")
    if failed:
        parts.append("Failed " + ", ".join(failed) + ".")
    if sync_failed:
        parts.append("ODI mapping synchronization failed.")
    return " ".join(parts) or "Column DDL failed."


def _sanitized_autofix_batch(result: dict) -> dict:
    columns = [
        _sanitized_autofix_column(item)
        for item in result.get("columns") or []
        if isinstance(item, dict)
    ]
    payload: dict = {"status": result.get("status"), "columns": columns}
    added = result.get("added_columns")
    if isinstance(added, list):
        payload["added_columns"] = [column for column in added if isinstance(column, str)]
    if result.get("status") == "success":
        if result.get("ok") is True:
            payload["ok"] = True
        if isinstance(result.get("message"), str):
            payload["message"] = result["message"]
        if "odi_status" in result:
            payload["odi_status"] = result.get("odi_status")
        return payload
    payload["error"] = _autofix_failure_message(
        columns,
        sync_failed=result.get("sync_failed") is True,
    )
    error_type = _safe_error_type(result.get("error_type"))
    if error_type:
        payload["error_type"] = error_type
    if result.get("sync_failed") is True:
        payload["sync_failed"] = True
    return payload


def _autofix_batch_response(result: dict) -> object:
    if result.get("status") in _AUTOFIX_BATCH_STATUSES:
        return jsonify(_sanitized_autofix_batch(result))
    return _result_response("quality all-column autofix", result)


@bp.route("/api/quality/validate", methods=["POST"])
@limiter.limit("30 per minute")
def api_quality_validate():
    app_module = get_app_module()
    body = request.get_json() or {}
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    extra_rules_raw = body.get("extra_rules", [])
    if not isinstance(extra_rules_raw, list):
        return jsonify({"error": "extra_rules must be a list."}), 400
    try:
        with app_module.DataQualityManager() as dqm:
            auto_rules = dqm.generate_rules(owner, table)
            if extra_rules_raw:
                from data_quality import QualityRule

                for rule in extra_rules_raw:
                    if not isinstance(rule, dict):
                        return jsonify({"error": "Invalid quality rule."}), 400
                    column = _valid_column(app_module, rule.get("column", ""))
                    if not column:
                        return jsonify({"error": "Invalid quality-rule column."}), 400
                    auto_rules.append(QualityRule(
                        rule_name=rule.get("rule_name", "CUSTOM_" + column),
                        column=column,
                        rule_type=rule.get("rule_type", "not_null"),
                        params=rule.get("params", {}),
                        severity=rule.get("severity", "ERROR"),
                    ))
            result = dqm.validate_table(owner, table, custom_rules=auto_rules)
            summary = result.summary()
            if summary.get("unsupported_rules"):
                summary["passed"] = False
                summary["status"] = "failed"
            return jsonify(summary)
    except Exception as exc:
        return redacted_write_error("quality validation", exc)


@bp.route("/api/quality/reconcile", methods=["POST"])
@limiter.limit("30 per minute")
def api_quality_reconcile():
    app_module = get_app_module()
    body = request.get_json() or {}
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    try:
        from reconciliator import reconcile_table

        result = reconcile_table(owner, table)
        return _result_response("quality reconciliation", result)
    except Exception as exc:
        return redacted_write_error("quality reconciliation", exc)


@bp.route("/api/quality/autofix-column", methods=["POST"])
@limiter.limit("10 per minute")
def api_quality_autofix_column():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "quality column autofix")
    if approval_error:
        return approval_error
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    column = _valid_column(app_module, body.get("column", ""))
    if not column:
        return jsonify({"error": "Table and column are required."}), 400
    try:
        from reconciliator import autofix_missing_column

        result = autofix_missing_column(owner, table, column)
        return _result_response("quality column autofix", result)
    except Exception as exc:
        return redacted_write_error("quality column autofix", exc)


@bp.route("/api/quality/autofix-all-columns", methods=["POST"])
@limiter.limit("5 per minute")
def api_quality_autofix_all_columns():
    """Return HTTP 200 with status success, partial, or failed.

    Partial and failed batches used to be HTTP 500. Clients that key off HTTP
    status must read the JSON status field. DqDashboard.jsx checks res.error
    and still surfaces the failure text.
    """
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "quality all-column autofix")
    if approval_error:
        return approval_error
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    columns = body.get("columns", [])
    if not columns or not isinstance(columns, list):
        return jsonify({"error": "Table and column list are required."}), 400
    normalized_columns = [_valid_column(app_module, column) for column in columns]
    if any(column is None for column in normalized_columns):
        return jsonify({"error": "Invalid column name."}), 400
    try:
        from reconciliator import autofix_all_missing_columns

        result = autofix_all_missing_columns(owner, table, normalized_columns)
        if not isinstance(result, dict):
            return _result_response("quality all-column autofix", result)
        return _autofix_batch_response(result)
    except Exception as exc:
        return redacted_write_error("quality all-column autofix", exc)


@bp.route("/api/quality/sync-mapping", methods=["POST"])
@limiter.limit("5 per minute")
def api_quality_sync_mapping():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "quality mapping sync")
    if approval_error:
        return approval_error
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    try:
        from reconciliator import sync_odi_mapping

        with groovy_approval_context(body.get("approved")):
            result = sync_odi_mapping(owner, table)
        return _result_response("quality mapping sync", result)
    except Exception as exc:
        return redacted_write_error("quality mapping sync", exc)


@bp.route("/api/quality/create-error-table", methods=["POST"])
@limiter.limit("5 per minute")
def api_quality_create_err():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "quality error table creation")
    if approval_error:
        return approval_error
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    try:
        with app_module.DataQualityManager() as dqm:
            dqm.create_error_table(owner, table)
            return jsonify({"ok": True, "error_table": f"{app_module.config.AUDIT_ORACLE_USER}.ERR_{table}"})
    except Exception as exc:
        return redacted_write_error("quality error table creation", exc)


@bp.route("/api/quality/create-all-error-tables", methods=["POST"])
@limiter.limit("3 per minute")
def api_quality_create_all_err():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "quality all-error-table creation")
    if approval_error:
        return approval_error
    owner, validation_error = _owner_from_body(app_module, body)
    if validation_error:
        return validation_error
    try:
        with app_module.DataQualityManager() as dqm:
            return jsonify(dqm.create_all_error_tables(owner))
    except Exception as exc:
        return redacted_write_error("quality error table creation", exc)


@bp.route("/api/quality/validate-insert", methods=["POST"])
@limiter.limit("10 per minute")
def api_quality_validate_insert():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "quality validate insert")
    if approval_error:
        return approval_error
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    max_errors = body.get("max_errors_per_rule", 100)
    if not isinstance(max_errors, int) or isinstance(max_errors, bool) or not 1 <= max_errors <= 1000:
        return jsonify({"error": "max_errors_per_rule must be between 1 and 1000."}), 400
    try:
        with app_module.DataQualityManager() as dqm:
            result = dqm.validate_and_insert_errors(owner, table, max_errors_per_rule=max_errors)
            return jsonify(result.summary())
    except Exception as exc:
        return redacted_write_error("quality validate insert", exc)


@bp.route("/api/skey/create", methods=["POST"])
@limiter.limit("5 per minute")
def api_skey_create():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "SKEY object creation")
    if approval_error:
        return approval_error
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    try:
        with app_module.SkeyManager() as skey_manager:
            return jsonify(skey_manager.create_skey_objects(owner, table))
    except Exception as exc:
        return redacted_write_error("SKEY object creation", exc)


@bp.route("/api/skey/expression", methods=["POST"])
def api_skey_expression():
    app_module = get_app_module()
    body = request.get_json() or {}
    owner, table, validation_error = _owner_table_from_body(app_module, body)
    if validation_error:
        return validation_error
    nk_expr = body.get("nk_expr", "")
    if not isinstance(nk_expr, str) or not nk_expr.strip():
        return jsonify({"error": "Table and nk_expr are required."}), 400
    try:
        with app_module.SkeyManager() as skey_manager:
            expr = skey_manager.get_skey_expression(owner, table, nk_expr)
            return jsonify({"expression": expr})
    except Exception as exc:
        return redacted_write_error("SKEY expression lookup", exc)
