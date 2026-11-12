from __future__ import annotations

import json

from flask import Blueprint, current_app, jsonify, request

from app_helpers import _group_odi_mapping_lineage_items
from routes.deps import get_app_module
from routes.error_responses import redacted_error, redacted_operation_result


bp = Blueprint("ops_read", __name__)
ODI_MAPPINGS_LIST_CAP = 500


@bp.route("/api/monitoring/freshness")
def api_monitoring_freshness():
    app_module = get_app_module()
    try:
        data_str = app_module.mon_data_freshness()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/monitoring/tablespace")
def api_monitoring_tablespace():
    app_module = get_app_module()
    try:
        data_str = app_module.mon_tablespace_usage()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/monitoring/etl-metrics")
def api_monitoring_etl_metrics():
    app_module = get_app_module()
    try:
        limit = request.args.get("limit", 20, type=int)
        data_str = app_module.mon_etl_job_metrics(limit=limit)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/sessions")
def api_dba_sessions():
    app_module = get_app_module()
    try:
        limit = request.args.get("limit", 30, type=int)
        data_str = app_module.dba_active_sessions(limit=limit)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/locks")
def api_dba_locks():
    app_module = get_app_module()
    try:
        data_str = app_module.dba_blocking_locks()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/tablespace")
def api_dba_tablespace():
    app_module = get_app_module()
    try:
        data_str = app_module.dba_tablespace_detail()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/privileges")
def api_dba_privileges():
    app_module = get_app_module()
    try:
        username = request.args.get("username", "")
        data_str = app_module.dba_user_privileges(username=username)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/object-privileges")
def api_dba_object_privileges():
    app_module = get_app_module()
    try:
        owner = request.args.get("owner", "DWH")
        obj = request.args.get("object_name", "")
        data_str = app_module.dba_object_privileges(owner=owner, object_name=obj)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/invalid-objects")
def api_dba_invalid_objects():
    app_module = get_app_module()
    try:
        owner = request.args.get("owner", "")
        data_str = app_module.dba_invalid_objects(owner=owner)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/redo-status")
def api_dba_redo_status():
    app_module = get_app_module()
    try:
        data_str = app_module.dba_redo_archive_status()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/backup-status")
def api_dba_backup_status():
    app_module = get_app_module()
    try:
        days = request.args.get("days", 7, type=int)
        data_str = app_module.dba_rman_backup_status(days=days)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/top-sql")
def api_dba_top_sql():
    app_module = get_app_module()
    try:
        order_by = request.args.get("order_by", "elapsed")
        limit = request.args.get("limit", 15, type=int)
        data_str = app_module.dba_top_sql(order_by=order_by, limit=limit)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/dba/wait-events")
def api_dba_wait_events():
    app_module = get_app_module()
    try:
        limit = request.args.get("limit", 20, type=int)
        data_str = app_module.dba_wait_events(limit=limit)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/lineage/<table_name>")
def api_lineage(table_name):
    app_module = get_app_module()
    try:
        with app_module.LineageManager() as lineage_manager:
            rows = lineage_manager.get_table_lineage(table_name)
            return jsonify(rows)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/lineage/impact/<source_table>")
def api_lineage_impact(source_table):
    app_module = get_app_module()
    direction = request.args.get("direction", "both").lower()
    if direction not in ("downstream", "upstream", "both"):
        direction = "both"
    try:
        with app_module.LineageManager() as lineage_manager:
            impacts = lineage_manager.get_impact_analysis(source_table, direction=direction)
            return jsonify(impacts)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/lineage/usage/<table_name>")
def api_lineage_usage(table_name):
    app_module = get_app_module()
    mapping_name = request.args.get("mapping_name")
    if not mapping_name:
        return jsonify({"error": "mapping_name is required."}), 400
    try:
        with app_module.LineageManager() as lineage_manager:
            details = lineage_manager.get_table_usage_details(table_name, mapping_name)
            try:
                items = app_module.json.loads(app_module.odi_get_mapping_details(mapping_name))
                if isinstance(items, list):
                    def _safe(value):
                        return "" if value is None else str(value).strip()

                    def _type_from_item(item):
                        data_type = _safe(item.get("DATA_TYPE") or item.get("FULL_DATA_TYPE"))
                        length = _safe(item.get("COL_LENGTH") or item.get("DATA_LENGTH"))
                        precision = _safe(item.get("DATA_PRECISION") or item.get("PRECISION"))
                        scale = _safe(item.get("DATA_SCALE") or item.get("SCALE"))
                        if data_type and "(" in data_type:
                            return data_type
                        if data_type:
                            size = precision or length
                            if scale:
                                size = f"{size},{scale}" if size else scale
                            return f"{data_type}({size})" if size else data_type
                        return ""

                    attr_types = {
                        _safe(item.get("ATTR_NAME")).upper(): _type_from_item(item)
                        for item in items
                        if _safe(item.get("ATTR_NAME"))
                    }
                    target_name = ""
                    target_hits = {}
                    for item in items:
                        qname = _safe(item.get("TABLE_QUALIFIED_NAME"))
                        cname = _safe(item.get("COMP_NAME"))
                        qname_up = qname.upper()
                        cname_up = cname.upper()
                        if (
                            qname_up.startswith(("DWH.", "ODS."))
                            or "ODS_" in qname_up
                            or cname_up.startswith(("ODS_", "TGT_"))
                        ):
                            key = qname or cname
                            target_hits[key] = target_hits.get(key, 0) + 1
                    if target_hits:
                        target_name = max(target_hits, key=target_hits.get)
                    if target_name:
                        parts = target_name.split(".")
                        if len(parts) > 1:
                            owner = parts[-2].upper()
                            tname = parts[-1]
                            if owner in {"TGT_DWH", "LGCL_ORACLE_DWH_UNIT", "ODS"}:
                                target_name = f"DWH.{tname}"
                            elif owner in {"SRC_HR", "HR"}:
                                target_name = f"HR.{tname}"
                            elif owner in {"SRC_SH", "SH"}:
                                target_name = f"SH.{tname}"
                        elif target_name.upper().startswith(("ODS_", "DWH_", "SKEY_")):
                            target_name = f"DWH.{target_name}"
                        schema_df = app_module.get_table_schema(target_name)
                        for _, row in schema_df.iterrows():
                            attr_types[_safe(row["COLUMN_NAME"]).upper()] = _safe(row["FULL_DATA_TYPE"])
                    component_type_maps = {}
                    for item in items:
                        qname = _safe(item.get("TABLE_QUALIFIED_NAME"))
                        cname = _safe(item.get("COMP_NAME"))
                        ctype = _safe(item.get("COMP_TYPE")).upper()
                        qname_up = qname.upper()
                        cname_up = cname.upper()
                        is_target = (
                            qname_up.startswith(("DWH.", "ODS."))
                            or "ODS_" in qname_up
                            or cname_up.startswith(("ODS_", "TGT_"))
                        )
                        if is_target or ctype not in {"FILE", "XML", "DATASTORE", "XSD"}:
                            continue
                        source_name = qname or cname
                        parts = source_name.split(".")
                        if len(parts) > 1:
                            owner = parts[-2].upper()
                            tname = parts[-1]
                            if owner in {"SRC_HR", "HR"}:
                                source_name = f"HR.{tname}"
                            elif owner in {"SRC_SH", "SH"}:
                                source_name = f"SH.{tname}"
                            elif owner in {"TGT_DWH", "LGCL_ORACLE_DWH_UNIT", "ODS"}:
                                source_name = f"DWH.{tname}"
                        try:
                            source_df = app_module.get_table_schema(source_name)
                            source_type_map = {
                                _safe(srow["COLUMN_NAME"]).upper(): _safe(srow["FULL_DATA_TYPE"])
                                for _, srow in source_df.iterrows()
                            }
                            if cname:
                                component_type_maps[cname.upper()] = source_type_map
                            if source_name:
                                component_type_maps[source_name.split(".")[-1].upper()] = source_type_map
                        except Exception:
                            pass
                    for row in details:
                        attr_name = _safe(row.get("attr_name")).upper()
                        data_type = attr_types.get(attr_name, "")
                        expression = _safe(row.get("expression"))
                        upper_expr = expression.upper()
                        if not data_type and upper_expr in {"SYSDATE", "CURRENT_DATE", "CURRENT_TIMESTAMP"}:
                            data_type = "DATE" if upper_expr != "CURRENT_TIMESTAMP" else "TIMESTAMP"
                        if not data_type and len(expression) >= 2 and expression[0] == expression[-1] == "'":
                            data_type = f"VARCHAR2({max(len(expression) - 2, 1)})"
                        if not data_type and "." in expression:
                            comp_name, col_name = expression.rsplit(".", 1)
                            lookup = component_type_maps.get(comp_name.upper())
                            if lookup:
                                data_type = lookup.get(col_name.upper(), "")
                        row["data_type"] = data_type
            except Exception:
                pass
            return jsonify(details)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/mapping/snapshots/<mapping_name>")
def api_mapping_snapshots(mapping_name):
    app_module = get_app_module()
    limit = request.args.get("limit", 10, type=int)
    try:
        with app_module.MappingSnapshotManager() as snapshot_manager:
            snapshots = snapshot_manager.get_snapshots(mapping_name, limit=limit)
            return jsonify(snapshots)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/mapping/compare/<mapping_name>")
def api_mapping_compare(mapping_name):
    app_module = get_app_module()
    snap_old = request.args.get("snap_old", type=int)
    snap_new = request.args.get("snap_new", type=int)
    try:
        with app_module.MappingSnapshotManager() as snapshot_manager:
            if snap_old and snap_new:
                diff = snapshot_manager.compare_snapshots(snap_old, snap_new)
            else:
                diff = snapshot_manager.compare_latest(mapping_name)
            return jsonify(diff)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/mapping/graph-info")
def api_mapping_graph_info():
    app_module = get_app_module()
    try:
        graph = app_module.get_graph(use_memory_checkpoint=False)
        compiled_graph = graph.get_graph()
        nodes = [name for name in compiled_graph.nodes.keys() if not name.startswith("__")]
        edges = [
            {"source": edge.source, "target": edge.target}
            for edge in compiled_graph.edges
            if not edge.source.startswith("__") and not edge.target.startswith("__")
        ]

        try:
            mermaid = compiled_graph.draw_mermaid()
        except Exception:
            mermaid = None

        return jsonify({
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "mermaid": mermaid,
            "orchestrator": "LangGraph",
            "checkpoint": "MemorySaver (in-memory)",
        })
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/mapping/runs")
def api_mapping_runs():
    app_module = get_app_module()
    raw_limit = request.args.get("limit", 50, type=int)
    limit = max(1, min(raw_limit or 50, 100))
    status = request.args.get("status")
    status = str(status).strip().upper() if status else None
    try:
        with app_module.MappingStateManager() as state_manager:
            runs = state_manager.list_recent_runs(limit=limit, status=status)
        return jsonify({"runs": runs, "count": len(runs), "limit": limit, "status": status})
    except Exception as exc:
        app_module.logger.error("Mapping run history read failed: %s", type(exc).__name__)
        return redacted_error("Operational read", exc)


@bp.route("/api/mapping/state/<mapping_name>")
def api_mapping_state(mapping_name):
    app_module = get_app_module()
    try:
        with app_module.MappingStateManager() as state_manager:
            state = state_manager.get_by_mapping(mapping_name)
        if not state:
            return jsonify({"mapping_name": mapping_name, "state": None}), 404
        return jsonify({"mapping_name": mapping_name, "state": state})
    except Exception as exc:
        app_module.logger.error("Mapping state read failed: %s", type(exc).__name__)
        return redacted_error("Operational read", exc)


@bp.route("/api/mapping-log/<batch_id>")
def api_mapping_log(batch_id):
    app_module = get_app_module()
    batch_id = str(batch_id or "").strip()
    if not app_module._valid_version(batch_id):
        return jsonify({"error": "Invalid batch_id."}), 400
    try:
        with app_module.RollbackManager() as rollback_manager:
            log = rollback_manager.get_batch_log(batch_id)
            return jsonify(redacted_operation_result(log, "mapping log read"))
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/alerts")
def api_alerts():
    app_module = get_app_module()
    try:
        with app_module.AlertManager() as alert_manager:
            alerts = alert_manager.get_unacknowledged()
            return jsonify(alerts)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/alerts/history")
def api_alerts_history():
    app_module = get_app_module()
    try:
        with app_module.AlertManager() as alert_manager:
            limit = request.args.get("limit", 50, type=int)
            history = alert_manager.get_history(limit=limit)
            return jsonify(history)
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/odi/scenarios")
def api_odi_scenarios():
    app_module = get_app_module()
    try:
        data_str = app_module.odi_get_scenario_list()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/odi/mappings")
def api_odi_mappings():
    app_module = get_app_module()
    raw_data = "[]"
    try:
        # One mapping past the cap: a full page is not truncation.
        raw_data = app_module.odi_get_mapping_lineage(limit=ODI_MAPPINGS_LIST_CAP + 1)
        items = json.loads(raw_data)
        if not isinstance(items, list):
            return current_app.response_class(response=raw_data, status=200, mimetype="application/json")
        grouped = _group_odi_mapping_lineage_items(items)
        truncated = len(grouped) > ODI_MAPPINGS_LIST_CAP
        if truncated:
            grouped = grouped[:ODI_MAPPINGS_LIST_CAP]
        response = jsonify(grouped)
        if truncated:
            response.headers["X-ODI-Mappings-Truncated"] = "1"
            response.headers["X-ODI-Mappings-Limit"] = str(ODI_MAPPINGS_LIST_CAP)
        return response
    except Exception as exc:
        app_module.logger.error("Critical mappings error: %s", type(exc).__name__)
        return current_app.response_class(response=raw_data, status=200, mimetype="application/json")


@bp.route("/api/odi/lineage")
def api_odi_lineage():
    app_module = get_app_module()
    try:
        data_str = app_module.odi_get_mapping_lineage(limit=50)
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)


@bp.route("/api/odi/kms")
def api_odi_kms():
    app_module = get_app_module()
    try:
        data_str = app_module.odi_get_knowledge_modules()
        return current_app.response_class(response=data_str, status=200, mimetype="application/json")
    except Exception as exc:
        return redacted_error("Operational read", exc)
