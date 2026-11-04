from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file

from routes.deps import get_app_module
from routes.error_responses import redacted_error


bp = Blueprint("schema_read", __name__)


@bp.route("/api/security/audit")
def api_security_audit():
    app_module = get_app_module()
    if not app_module._auth_enabled():
        return jsonify({"error": "Authentication must be enabled to view security audit."}), 403
    if not app_module._has_role("admin"):
        app_module._record_security_event("audit_view_denied", "blocked", required_role="admin")
        return jsonify({"error": "Admin role is required."}), 403
    limit = min(max(int(request.args.get("limit", "100") or 100), 1), 200)
    events, chain_valid = app_module._read_security_audit_events(limit=limit)
    app_module._record_security_event("audit_view", "allowed", limit=limit)
    return jsonify({
        "events": events,
        "chain_valid": chain_valid,
        "limit": limit,
    })


@bp.route("/api/schemas")
def api_schemas():
    app_module = get_app_module()
    try:
        return jsonify(list(app_module.config.PROJECT_SCHEMAS))
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/tables")
def api_tables():
    app_module = get_app_module()
    try:
        project = app_module.config.PROJECT_SCHEMAS or ["HR", "SH", "DWH"]
        selected_owner = (request.args.get("schema") or "").strip().upper()
        owners = [selected_owner] if selected_owner and selected_owner in project else list(project)
        owner_list = ",".join("'" + owner.replace("'", "") + "'" for owner in owners)
        source_set = set(app_module.config.SOURCE_ORACLE_SCHEMAS or [])

        sql = f"""
            SELECT OWNER, TABLE_NAME
            FROM ALL_TABLES
            WHERE OWNER IN ({owner_list})
              AND TABLE_NAME NOT LIKE 'DR$%'
              AND TABLE_NAME NOT LIKE 'BIN$%'
              AND NESTED = 'NO' AND SECONDARY = 'N'
            ORDER BY OWNER, TABLE_NAME
        """
        df = app_module.execute_select(sql)

        tables = []
        for _, row in df.iterrows():
            owner = str(row["OWNER"])
            table_name = str(row["TABLE_NAME"])
            full_name = f"{owner}.{table_name}"
            role = "SOURCE" if owner in source_set else "ORACLE"
            tables.append({
                "name": f"{role}: {full_name}",
                "original": full_name,
                "platform": role,
                "schema": owner,
            })

        comments = {}
        try:
            comment_df = app_module.execute_select(
                f"SELECT OWNER||'.'||TABLE_NAME AS FULL_NAME, COMMENTS "
                f"FROM ALL_TAB_COMMENTS WHERE OWNER IN ({owner_list})"
            )
            for _, row in comment_df.iterrows():
                value = row["COMMENTS"]
                comments[row["FULL_NAME"]] = "" if app_module.pd.isna(value) else str(value)
        except Exception:
            pass
        for table in tables:
            table["comment"] = comments.get(table["original"], "")

        return jsonify(tables)
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/impact")
def api_schema_impact():
    app_module = get_app_module()
    object_ref = request.args.get("object", "")
    try:
        return jsonify(app_module._schema_impact_payload(object_ref))
    except ValueError as exc:
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/drift")
def api_schema_drift():
    app_module = get_app_module()
    owner = (request.args.get("owner") or "DWH").strip().upper()
    if not app_module._validate_owner(owner):
        return jsonify({"error": f"Invalid owner: {owner}"}), 400
    try:
        drift = app_module.detect_db_odi_type_drift(owner)
        return jsonify({"owner": owner, "drift_count": len(drift), "drift": drift, "read_only": True})
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/findings")
def api_schema_findings():
    app_module = get_app_module()
    owner = (request.args.get("owner") or "DWH").strip().upper()
    if not app_module._validate_owner(owner):
        return jsonify({"error": f"Invalid owner: {owner}"}), 400
    try:
        return jsonify(app_module.build_schema_sync_findings(owner))
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/findings/export")
def api_schema_findings_export():
    app_module = get_app_module()
    owner = (request.args.get("owner") or "DWH").strip().upper()
    if not app_module._validate_owner(owner):
        return jsonify({"error": f"Invalid owner: {owner}"}), 400
    try:
        report = app_module.build_schema_sync_findings(owner)
        fields = [
            "kind",
            "severity",
            "owner",
            "table",
            "column",
            "db_type",
            "odi_type",
            "db_length",
            "odi_length",
            "db_precision",
            "odi_precision",
            "db_scale",
            "odi_scale",
            "mapping_name",
            "summary",
            "next_step",
        ]
        text_buffer = io.StringIO()
        writer = csv.DictWriter(text_buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for finding in report.get("findings", []):
            writer.writerow({field: finding.get(field, "") for field in fields})

        byte_buffer = io.BytesIO(text_buffer.getvalue().encode("utf-8-sig"))
        filename = f"schema_findings_{owner}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return send_file(
            byte_buffer,
            mimetype="text/csv",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/source-target/findings")
def api_schema_source_target_findings():
    app_module = get_app_module()
    source_schema = (request.args.get("source_schema") or "HR").strip().upper()
    target_owner = (request.args.get("target_owner") or "DWH").strip().upper()
    if not app_module._valid_identifier(source_schema):
        return jsonify({"error": f"Invalid source_schema: {source_schema}"}), 400
    if not app_module._validate_owner(target_owner):
        return jsonify({"error": f"Invalid target_owner: {target_owner}"}), 400
    try:
        return jsonify(app_module.build_source_target_metadata_findings(source_schema, target_owner))
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/pk-parity")
def api_schema_pk_parity():
    app_module = get_app_module()
    source_schema = (request.args.get("source_schema") or "HR").strip().upper()
    target_owner = (request.args.get("target_owner") or "DWH").strip().upper()
    if not app_module._valid_identifier(source_schema):
        return jsonify({"error": f"Invalid source_schema: {source_schema}"}), 400
    if not app_module._validate_owner(target_owner):
        return jsonify({"error": f"Invalid target_owner: {target_owner}"}), 400
    try:
        requested_max_keys = int(request.args.get("max_keys") or app_module.PK_PARITY_MAX_KEYS)
        if requested_max_keys <= 0:
            return jsonify({"error": "max_keys must be positive."}), 400
        max_keys = min(requested_max_keys, app_module.PK_PARITY_MAX_KEYS)
        return jsonify(app_module.build_pk_parity_report(source_schema, target_owner, max_keys=max_keys))
    except ValueError as exc:
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema/<path:table_name>")
def api_schema(table_name):
    app_module = get_app_module()
    try:
        df = app_module.get_table_schema(table_name)
        cols = df[["COLUMN_NAME", "FULL_DATA_TYPE", "NULLABLE"]].rename(
            columns={"FULL_DATA_TYPE": "DATA_TYPE"}
        ).to_dict(orient="records")
        return jsonify(cols)
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/source/schema/<path:table_name>")
@bp.route("/api/mssql/schema/<path:table_name>")
def api_source_schema(table_name):
    app_module = get_app_module()
    try:
        df = app_module.get_source_table_schema(table_name)
        rows = [
            {
                "COLUMN_NAME": row["COLUMN_NAME"],
                "DATA_TYPE": row["FULL_DATA_TYPE"],
                "NULLABLE": "",
                "COMMENTS": "" if app_module.pd.isna(row.get("COMMENTS")) else str(row.get("COMMENTS") or ""),
            }
            for _, row in df.iterrows()
        ]
        return jsonify(rows)
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/preview/<path:table_name>")
def api_preview(table_name):
    app_module = get_app_module()
    table_name_upper = table_name.upper()
    if not re.match(r"^[A-Z0-9_]+\.[A-Z0-9_]+$", table_name_upper):
        return jsonify({"error": "Invalid table name format."}), 400
    owner_part = table_name_upper.split(".")[0]
    if not app_module._validate_owner(owner_part):
        return jsonify({"error": f"Invalid owner: {owner_part}"}), 400
    try:
        sql = app_module._build_table_preview_sql(table_name_upper, 3)
        df = app_module.execute_select(sql)
        return jsonify({
            "columns": list(df.columns),
            "rows": app_module.df_to_jsonable(df),
        })
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/source/preview/<path:table_name>")
def api_source_preview(table_name):
    app_module = get_app_module()
    if not re.match(r"^[A-Za-z0-9_$]+\.[A-Za-z0-9_$]+$", table_name):
        return jsonify({"error": "Invalid table name format."}), 400
    try:
        from source_connector import get_source_connector

        df = get_source_connector().sample_data(table_name, 3)
        return jsonify({
            "columns": list(df.columns),
            "rows": app_module.df_to_jsonable(df),
        })
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/schema-changes")
def api_schema_changes():
    app_module = get_app_module()
    try:
        changes = app_module.get_recent_changes(limit=20)
        for change in changes:
            for key, value in change.items():
                if hasattr(value, "isoformat"):
                    change[key] = value.isoformat()
        return jsonify({"changes": changes})
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/watermarks")
def api_watermarks():
    app_module = get_app_module()
    try:
        with app_module.WatermarkManager() as watermark_manager:
            marks = watermark_manager.list_all_watermarks()
            for mark in marks:
                for key, value in mark.items():
                    if hasattr(value, "isoformat"):
                        mark[key] = value.isoformat()
            return jsonify({"watermarks": marks})
    except Exception as exc:
        return redacted_error("Schema read", exc)


@bp.route("/api/incremental/preflight")
def api_incremental_preflight():
    app_module = get_app_module()
    try:
        spec_name = app_module._validate_incremental_spec_name(request.args.get("spec_name"))
        report = app_module.run_configured_incremental_strategy_preflight([spec_name])
        return jsonify(report)
    except ValueError as exc:
        return redacted_error("Schema request rejected", exc, status=400)
    except Exception as exc:
        app_module.logger.error("Incremental preflight failed: %s", type(exc).__name__)
        return jsonify({
            "error": "Incremental preflight failed.",
            "error_type": type(exc).__name__,
            "raw_rows_included": False,
            "secret_printed": False,
        }), 500
