from __future__ import annotations

from flask import Blueprint, jsonify, request

from routes.deps import get_app_module
from routes.error_responses import redacted_error, redacted_write_error


bp = Blueprint("schema_write", __name__)
limiter = get_app_module().limiter


@bp.route("/api/schema/change/preflight", methods=["POST"])
@limiter.limit("20 per minute")
def api_schema_change_preflight():
    app_module = get_app_module()
    try:
        return jsonify(app_module._schema_change_preflight_payload(request.get_json() or {}))
    except ValueError as exc:
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        return redacted_write_error("schema change preflight", exc)


@bp.route("/api/schema/change/apply-db", methods=["POST"])
@limiter.limit("5 per minute")
def api_schema_change_apply_db():
    app_module = get_app_module()
    try:
        payload = app_module._schema_change_apply_db_payload(request.get_json() or {})
        status = 400 if payload.get("status") == "BLOCKED" else 200
        return jsonify(payload), status
    except ValueError as exc:
        if str(exc).startswith("approved:true is required"):
            return jsonify({"error": "approved:true is required for DB apply."}), 400
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        return redacted_write_error("schema change apply", exc)


@bp.route("/api/schema/change/sync-odi", methods=["POST"])
@limiter.limit("5 per minute")
def api_schema_change_sync_odi():
    app_module = get_app_module()
    try:
        return jsonify(app_module._schema_change_sync_odi_payload(request.get_json() or {}))
    except ValueError as exc:
        if str(exc).startswith("approved:true is required"):
            return jsonify({"error": "approved:true is required for ODI metadata sync."}), 400
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        return redacted_write_error("schema change ODI sync", exc)


@bp.route("/api/schema/change/reconcile-scenario", methods=["POST"])
@limiter.limit("5 per minute")
def api_schema_change_reconcile_scenario():
    app_module = get_app_module()
    try:
        payload = app_module._schema_change_reconcile_scenario_payload(request.get_json() or {})
        status = 500 if payload.get("status") == "SCENARIO_RECONCILE_FAILED" else 200
        return jsonify(payload), status
    except ValueError as exc:
        if str(exc).startswith("approved:true is required"):
            return jsonify({"error": "approved:true is required for scenario reconcile."}), 400
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        return redacted_write_error("schema change scenario reconcile", exc)


@bp.route("/api/schema-changes/detect", methods=["POST"])
@limiter.limit("5 per minute")
def api_detect_schema_changes():
    app_module = get_app_module()
    body = request.get_json() or {}
    owner = body.get("owner", "DWH").upper()
    if not app_module._validate_owner(owner):
        return jsonify({"error": f"Invalid owner: {owner}"}), 400
    approval_error = app_module._approval_error(body, "schema change detection")
    if approval_error:
        return approval_error
    try:
        changes = app_module.detect_changes(owner)
        return jsonify({"detected": len(changes), "changes": changes})
    except Exception as exc:
        return redacted_write_error("schema changes detect", exc)


@bp.route("/api/watermarks/delta", methods=["POST"])
def api_watermark_delta():
    app_module = get_app_module()
    body = request.get_json() or {}
    owner = body.get("owner", "DWH").upper()
    if not app_module._validate_owner(owner):
        return jsonify({"error": f"Invalid owner: {owner}"}), 400
    table = body.get("table", "")
    date_col = body.get("date_column", "MODIFIED_DATE")
    if not table:
        return jsonify({"error": "Table is required."}), 400
    try:
        with app_module.WatermarkManager() as watermark_manager:
            sql = watermark_manager.get_delta_query(owner, table, date_col)
            return jsonify({"delta_sql": sql, "table": f"{owner}.{table}"})
    except Exception as exc:
        return redacted_write_error("watermark delta", exc)


@bp.route("/api/incremental/execute", methods=["POST"])
@limiter.limit("5 per minute")
def api_incremental_execute():
    """Execute an approved incremental append after rerun and gate checks."""

    app_module = get_app_module()
    body = request.get_json() or {}
    try:
        spec_name = app_module._validate_incremental_spec_name(body.get("spec_name"))
        report = app_module.run_configured_incremental_strategy_preflight([spec_name])
        preflight_result = (report.get("results") or [{}])[0]
        execution_request = app_module.build_incremental_execution_request(
            preflight_result,
            approved=body.get("approved") is True,
        )
        response_base = {
            "preflight": report,
            "execution": execution_request,
            "raw_rows_included": False,
            "secret_printed": False,
        }
        if execution_request.get("status") != "ready_for_executor":
            return jsonify(response_base)

        gate_results = app_module._post_load_gate_results(body)
        if not app_module._post_load_gates_confirmed(gate_results):
            return jsonify({
                **response_base,
                "error": "post_load_gate_results must confirm golden record and DQ gates before live append execution.",
            }), 400

        target_conn = app_module._open_incremental_target_connection()
        try:
            with app_module.WatermarkManager() as watermark_writer:
                execution = app_module.execute_incremental_append(
                    preflight_result,
                    approved=True,
                    target_conn=target_conn,
                    watermark_writer=watermark_writer,
                    post_load_gate_results=gate_results,
                )
        finally:
            target_conn.close()

        return jsonify({
            "preflight": report,
            "execution": execution,
            "raw_rows_included": False,
            "secret_printed": False,
        })
    except ValueError as exc:
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        app_module.logger.error("Incremental execution failed: %s", type(exc).__name__)
        return jsonify({
            "error": "Incremental execution failed.",
            "error_type": type(exc).__name__,
            "raw_rows_included": False,
            "secret_printed": False,
        }), 500
