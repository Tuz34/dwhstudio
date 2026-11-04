from __future__ import annotations

import io
import json
from datetime import datetime

import pandas as pd
from flask import Blueprint, current_app, jsonify, request, send_file, session

from mapping_detail_presenter import present_mapping_detail
from odi_orchestrator import groovy_approval_context
from routes.deps import get_app_module
from routes.error_responses import redacted_write_error

bp = Blueprint("misc_write", __name__)
limiter = get_app_module().limiter


@bp.route("/api/execute", methods=["POST"])
@limiter.limit("30 per minute")
def api_execute():
    app_module = get_app_module()
    body = request.get_json() or {}
    sql = body.get("sql", "")
    params = body.get("params", {})
    try:
        clean_sql = sql.strip()
        upper_sql = clean_sql.upper()
        if upper_sql.startswith("SELECT") or upper_sql.startswith("WITH"):
            if clean_sql.endswith(";"):
                clean_sql = clean_sql[:-1].strip()
            max_rows = 100
            try:
                df = app_module.execute_select(f"SELECT * FROM (\n{clean_sql}\n) WHERE ROWNUM <= {max_rows}")
                limited = True
            except Exception:
                df = app_module.execute_select(clean_sql).head(max_rows)
                limited = len(df.index) >= max_rows
            return jsonify({
                "result": f"Query returned {len(df.index)} row(s).",
                "query": {
                    "action": "select",
                    "label": body.get("label") or "Query Result",
                    "sql": clean_sql,
                    "params": params or {},
                    "result": {
                        "columns": list(df.columns),
                        "rows": app_module.df_to_jsonable(df),
                    },
                    "preview_limited": limited,
                    "preview_rows": max_rows,
                }
            })
        approval_error = app_module._approval_error(body, "DML/DDL execution")
        if approval_error:
            return approval_error
        msg = app_module.execute_dml(sql, params if params else None)
        return jsonify({"result": msg})
    except Exception as exc:
        return redacted_write_error("SQL execution", exc)


@bp.route("/api/export", methods=["POST"])
def api_export():
    body = request.get_json()
    headers = body.get("headers", [])
    rows = body.get("rows", [])

    df = pd.DataFrame(rows, columns=headers)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Rapor")
    buf.seek(0)

    fname = f"rapor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


@bp.route("/api/clear", methods=["POST"])
def api_clear():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/monitoring/dq-scores")
@limiter.limit("10 per minute")
def api_monitoring_dq_scores():
    """Tablo bazli DQ skorlari. ?owner=DWH&table=SALES (table opsiyonel)."""
    app_module = get_app_module()
    try:
        owner = request.args.get("owner", "DWH").upper()
        table = request.args.get("table", "").upper()

        try:
            data_str = app_module.mon_dq_score(owner=owner, table_name=table)
            if not data_str or data_str.strip() == "":
                return jsonify([])
            return current_app.response_class(response=data_str, status=200, mimetype="application/json")
        except Exception as inner_exc:
            app_module.logger.error(
                "mon_dq_score critical error: %s", type(inner_exc).__name__
            )
            return jsonify([])
    except Exception as exc:
        app_module.logger.error(
            "api_monitoring_dq_scores error: %s", type(exc).__name__
        )
        return redacted_write_error("monitoring DQ scores", exc)


@bp.route("/api/lineage/refresh", methods=["POST"])
@limiter.limit("3 per minute")
def api_lineage_refresh():
    """ODI'dan lineage verilerini gunceller."""
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "lineage refresh")
    if approval_error:
        return approval_error
    try:
        with app_module.LineageManager() as lm:
            result = lm.populate_from_odi()
            return jsonify(result)
    except Exception as exc:
        return redacted_write_error("lineage refresh", exc)


@bp.route("/api/alerts/acknowledge", methods=["POST"])
@limiter.limit("10 per minute")
def api_alerts_acknowledge():
    """Tek veya toplu alert onayi."""
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "alert acknowledgement")
    if approval_error:
        return approval_error
    alert_id = body.get("alert_id")
    acknowledge_all = body.get("all") is True

    try:
        with app_module.AlertManager() as am:
            if acknowledge_all:
                count = am.acknowledge_all()
                return jsonify({"ok": True, "acknowledged": count})
            if alert_id:
                ok = am.acknowledge(int(alert_id))
                return jsonify({"ok": ok})
            return jsonify({"error": "alert_id or all=true is required."}), 400
    except Exception as exc:
        return redacted_write_error("alert acknowledge", exc)


@bp.route("/api/alerts/check", methods=["POST"])
@limiter.limit("5 per minute")
def api_alerts_check():
    """Otomatik kontrol calistir (tazelik + sema degisikligi)."""
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "alert check")
    if approval_error:
        return approval_error
    freshness_days = body.get("freshness_days", 2)

    try:
        with app_module.AlertManager() as am:
            created = am.check_and_alert(freshness_days=freshness_days)
            return jsonify({"created_alerts": created, "count": len(created)})
    except Exception as exc:
        return redacted_write_error("alert check", exc)


@bp.route("/api/odi/sessions")
@limiter.limit("60 per minute")
def api_odi_sessions():
    app_module = get_app_module()
    try:
        data_str = app_module.odi_get_recent_sessions(limit=50)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_write_error("ODI sessions lookup", exc)


@bp.route("/api/odi/session_suggestion", methods=["POST"])
def api_odi_session_suggestion():
    try:
        data = request.get_json()
        error_msg = data.get("error_message", "")
        if not error_msg or error_msg == "null":
            return jsonify({"suggestion": None})

        from rag_manager import search_documents

        suggestions = search_documents("error_patterns", error_msg, n_results=1)

        if suggestions:
            return jsonify({"suggestion": suggestions[0]})
        return jsonify({"suggestion": "No registered RAG solution found for this error."})
    except Exception as exc:
        return redacted_write_error("ODI session suggestion", exc)


@bp.route("/api/odi/mapping_detail")
@limiter.limit("60 per minute")
def api_odi_mapping_detail():
    app_module = get_app_module()
    try:
        mapping_name = request.args.get("mapping_name")
        if not mapping_name:
            return jsonify({"error": "mapping_name is required"}), 400
        data_str = app_module.odi_get_mapping_details(mapping_name)
        presentation = present_mapping_detail(
            mapping_name,
            data_str,
            app_module.get_table_schema,
            app_module._read_mapping_km_options,
        )
        if presentation.kind == "raw":
            return current_app.response_class(
                response=presentation.raw_json,
                status=200,
                mimetype="application/json",
            )
        if presentation.kind == "parse_error":
            return jsonify({"error": presentation.raw_json}), 200
        return jsonify(presentation.payload)
    except Exception as exc:
        return redacted_write_error("ODI mapping detail", exc)


@bp.route("/api/airflow/run-odi", methods=["POST"])
@limiter.limit("5 per minute")
def api_airflow_run_odi():
    """Airflow (Docker) uzerinden istenilen ODI senaryosunu tetikler."""
    app_module = get_app_module()
    try:
        body = request.get_json() or {}
        approval_error = app_module._approval_error(body, "legacy ODI scenario run")
        if approval_error:
            return approval_error
        scen_name = str(body.get("scen_name", "MAP_CUSTOMER_TO_ODS_SCN")).strip().upper()
        if not app_module._valid_identifier(scen_name):
            return jsonify({"error": "Invalid scen_name."}), 400
        script_path = app_module.os.path.join(
            app_module.os.path.dirname(app_module.__file__),
            "odi_automation",
            "full_odi_run.py",
        )
        result = app_module.subprocess.run(
            [app_module.sys.executable, script_path, scen_name],
            capture_output=True,
            text=True,
            timeout=900,
        )
        return jsonify({
            "stdout": app_module.redact_sensitive_text(result.stdout, max_len=12000),
            "stderr": app_module.redact_sensitive_text(result.stderr, max_len=12000),
            "code": result.returncode,
        }), 200 if result.returncode == 0 else 500
    except app_module.subprocess.TimeoutExpired:
        return jsonify({"error": "Legacy ODI scenario run timed out."}), 504
    except Exception as exc:
        return redacted_write_error("legacy ODI scenario run", exc)


@bp.route("/api/airflow/run-mapping", methods=["POST"])
@limiter.limit("5 per minute")
def api_airflow_run_mapping():
    """Run a single ODI mapping through the legacy Airflow-compatible API."""
    app_module = get_app_module()
    try:
        body = request.get_json() or {}
        approval_error = app_module._approval_error(body, "legacy ODI mapping run")
        if approval_error:
            return approval_error
        mapping_name = body.get("mapping_name", "").strip().upper()
        version = str(body.get("scenario_version", "001")).strip() or "001"

        if not mapping_name:
            return jsonify({"status": "error", "error": "mapping_name is required."}), 400
        if not app_module._valid_identifier(mapping_name):
            return jsonify({"status": "error", "error": "Invalid mapping_name."}), 400
        if not app_module._valid_version(version):
            return jsonify({"status": "error", "error": "Invalid scenario_version."}), 400

        from odi_orchestrator import OdiOrchestrator

        orch = OdiOrchestrator()
        scen_name = f"{mapping_name}_SCN"

        with groovy_approval_context(body.get("approved")):
            gen_res = orch.generate_scenario(scen_name, mapping_name, version)
            if "Hata:" in gen_res:
                return jsonify({"status": "error", "error": "Scenario generation failed."}), 500

            run_res = orch.run_scenario(scen_name, version)
        if "Hata:" in run_res:
            return jsonify({"status": "error", "error": "Scenario run failed."}), 500

        return jsonify({
            "status": "success",
            "scenario": scen_name,
            "version": version,
            "log": app_module.redact_sensitive_text(run_res, max_len=12000),
        })
    except Exception as exc:
        return redacted_write_error("legacy ODI mapping run", exc)


@bp.route("/api/airflow/run-dq", methods=["POST"])
@limiter.limit("5 per minute")
def api_airflow_run_dq():
    """Airflow uzerinden Data Quality validation tetikler."""
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "legacy DQ run")
    if approval_error:
        return approval_error
    owner = body.get("owner", "DWH").upper()
    table = body.get("table", "").strip().upper()
    if not table:
        return jsonify({"error": "Table is required."}), 400
    if not app_module._validate_owner(owner):
        return jsonify({"error": "Invalid owner."}), 400
    if not app_module._valid_identifier(table):
        return jsonify({"error": "Invalid table."}), 400
    try:
        with app_module.DataQualityManager() as dqm:
            res = dqm.validate_and_insert_errors(owner, table, max_errors_per_rule=100)
            return jsonify(res.summary()), 200
    except Exception as exc:
        return redacted_write_error("legacy DQ run", exc)
