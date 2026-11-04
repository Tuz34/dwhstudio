from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from mapping_run_service import execute_mapping_run, validate_mapping_run_options
from odi_orchestrator import groovy_approval_context
from routes.deps import get_app_module
from routes.error_responses import redacted_operation_result, redacted_write_error

bp = Blueprint("mapping_write", __name__)
limiter = get_app_module().limiter


@bp.route("/api/mapping/snapshot", methods=["POST"])
@limiter.limit("10 per minute")
def api_mapping_snapshot():
    app_module = get_app_module()
    body = request.get_json() or {}
    approval_error = app_module._approval_error(body, "mapping snapshot")
    if approval_error:
        return approval_error
    raw_mapping_name = body.get("mapping_name", "")
    mapping_name = raw_mapping_name.strip().upper() if isinstance(raw_mapping_name, str) else ""
    mapping_data = body.get("mapping_data", [])
    raw_label = body.get("label", "")
    label = raw_label.strip()[:100] if isinstance(raw_label, str) else ""
    if not app_module._valid_identifier(mapping_name):
        return jsonify({"error": "Invalid mapping_name."}), 400
    if not isinstance(mapping_data, (list, dict)):
        return jsonify({"error": "mapping_data must be a list or object."}), 400
    try:
        with app_module.MappingSnapshotManager() as msm:
            msm.ensure_table()
            snap_id = msm.take_snapshot(mapping_name, mapping_data, label)
            return jsonify({"ok": True, "snap_id": snap_id, "mapping_name": mapping_name})
    except Exception as exc:
        return redacted_write_error("mapping snapshot", exc)


@bp.route("/api/mapping/analyze-prompt", methods=["POST"])
@limiter.limit("20 per minute")
def api_mapping_analyze_prompt():
    app_module = get_app_module()
    body = request.get_json() or {}
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required."}), 400
    try:
        with app_module.MappingGenerator() as mg:
            result = mg.analyze_prompt(prompt)
            return jsonify(result)
    except Exception as exc:
        return redacted_write_error("mapping prompt analysis", exc)


@bp.route("/api/mapping/target-preflight", methods=["POST"])
@limiter.limit("20 per minute")
def api_mapping_target_preflight():
    app_module = get_app_module()
    body = request.get_json() or {}
    params = body.get("params")
    if params is None and any(k in body for k in ("src_table", "tgt_table", "mapping_name")):
        params = body
    if not params:
        return jsonify({"error": "params are required."}), 400

    try:
        with app_module.MappingGenerator() as mg:
            return jsonify(mg.target_table_preflight(params))
    except Exception as exc:
        app_module.logger.error(
            "api_mapping_target_preflight failed: %s", type(exc).__name__
        )
        return redacted_write_error("mapping target preflight", exc)


@bp.route("/api/mapping/generate", methods=["POST"])
@limiter.limit("10 per minute")
def api_mapping_generate():
    app_module = get_app_module()
    body = request.get_json() or {}
    prompt = body.get("prompt", "").strip()
    params = body.get("params")
    if params is None and any(k in body for k in ("src_table", "tgt_table", "mapping_name")):
        params = body

    if not prompt and not params:
        return jsonify({"error": "prompt or params are required."}), 400
    approval_error = app_module._approval_error(body, "ODI mapping generation")
    if approval_error:
        return approval_error

    engine = (request.args.get("engine") or body.get("engine") or "graph").strip().lower()

    try:
        with groovy_approval_context(body.get("approved")):
            if engine == "classic":
                with app_module.MappingGenerator() as mg:
                    if params:
                        result = mg.generate_from_params(params)
                    else:
                        result = mg.generate_from_prompt(prompt)
                    return jsonify(redacted_operation_result(result, "mapping generation"))

            require_approval = body.get("require_approval") is True
            thread_id = body.get("thread_id")
            result = app_module.run_mapping_graph(
                user_prompt=prompt if prompt else None,
                params=params,
                require_approval=require_approval,
                thread_id=thread_id,
            )
        clean_result = {}
        for key, value in result.items():
            try:
                json.dumps(value)
                clean_result[key] = value
            except (TypeError, ValueError):
                clean_result[key] = str(value)
        clean_result.setdefault("engine", "graph")
        return jsonify(redacted_operation_result(clean_result, "mapping generation"))
    except Exception as exc:
        app_module.logger.error("api_mapping_generate failed: %s", type(exc).__name__)
        return redacted_write_error("mapping generation", exc)


@bp.route("/api/mapping/validate-expression", methods=["POST"])
@limiter.limit("30 per minute")
def api_validate_expression():
    body = request.get_json() or {}
    target_table = body.get("target_table", "").strip().upper()
    target_owner = body.get("target_owner", "DWH").strip().upper()
    expressions = body.get("expressions", {})
    if not target_table:
        return jsonify({"error": "target_table is required."}), 400
    if not expressions:
        return jsonify({"error": "expressions are required."}), 400
    try:
        from odi_orchestrator import OdiOrchestrator

        orch = OdiOrchestrator()
        result = orch.validate_expression(target_table, target_owner, expressions)
        return jsonify(result)
    except Exception as exc:
        return redacted_write_error("mapping expression validation", exc)


@bp.route("/api/mapping/update", methods=["POST"])
@limiter.limit("10 per minute")
def api_mapping_update():
    app_module = get_app_module()
    body = request.get_json() or {}
    mapping_name = body.get("mapping_name", "").strip().upper()
    changes = body.get("changes", {})
    if not mapping_name:
        return jsonify({"error": "mapping_name is required."}), 400
    if not app_module._valid_identifier(mapping_name):
        return jsonify({"error": "Invalid mapping_name."}), 400
    if not changes:
        return jsonify({"error": "changes are required."}), 400
    approval_error = app_module._approval_error(body, "ODI mapping update")
    if approval_error:
        return approval_error
    try:
        with groovy_approval_context(body.get("approved")):
            with app_module.MappingGenerator() as mg:
                result = mg.update_mapping(mapping_name, changes)
                return jsonify(redacted_operation_result(result, "mapping update"))
    except Exception as exc:
        return redacted_write_error("mapping update", exc)


@bp.route("/api/mapping/delete", methods=["POST"])
@limiter.limit("5 per minute")
def api_mapping_delete():
    app_module = get_app_module()
    body = request.get_json() or {}
    mapping_name = body.get("mapping_name", "").strip().upper()
    tgt_owner = body.get("tgt_owner", "DWH").strip().upper()
    if not mapping_name:
        return jsonify({"error": "mapping_name is required."}), 400
    if not app_module._valid_identifier(mapping_name):
        return jsonify({"error": "Invalid mapping_name."}), 400
    if not app_module._validate_owner(tgt_owner):
        return jsonify({"error": "Invalid target owner."}), 400
    approval_error = app_module._approval_error(body, "ODI mapping delete")
    if approval_error:
        return approval_error
    try:
        with groovy_approval_context(body.get("approved")):
            with app_module.MappingGenerator() as mg:
                result = mg.delete_mapping(mapping_name, tgt_owner)
                return jsonify(redacted_operation_result(result, "mapping delete"))
    except Exception as exc:
        return redacted_write_error("mapping delete", exc)


@bp.route("/api/mapping/run", methods=["POST"])
@limiter.limit("5 per minute")
def api_mapping_run():
    """Generate and run an ODI scenario for a mapping."""
    app_module = get_app_module()
    body = request.get_json() or {}
    mapping_name = body.get("mapping_name", "").strip().upper()
    if not mapping_name:
        return jsonify({"error": "mapping_name is required."}), 400
    if not app_module._valid_identifier(mapping_name):
        return jsonify({"error": "Invalid mapping_name."}), 400
    approval_error = app_module._approval_error(body, "ODI scenario run")
    if approval_error:
        return approval_error
    options, validation_error = validate_mapping_run_options(
        body,
        mapping_name,
        app_module._valid_version,
        app_module._valid_identifier,
    )
    if validation_error or options is None:
        return jsonify({"error": validation_error}), 400
    try:
        with groovy_approval_context(body.get("approved")):
            outcome = execute_mapping_run(app_module, options)
        return jsonify(outcome.payload), outcome.status_code
    except Exception as exc:
        return redacted_write_error("mapping run", exc)


@bp.route("/api/mapping/generate-graph", methods=["POST"])
@limiter.limit("10 per minute")
def api_mapping_generate_graph():
    app_module = get_app_module()
    body = request.get_json() or {}
    prompt = body.get("prompt", "").strip()
    params = body.get("params")
    require_approval = body.get("require_approval", False)
    thread_id = body.get("thread_id")

    if not prompt and not params:
        return jsonify({"error": "prompt or params are required."}), 400
    approval_error = app_module._approval_error(body, "ODI mapping graph generation")
    if approval_error:
        return approval_error

    try:
        with groovy_approval_context(body.get("approved")):
            result = app_module.run_mapping_graph(
                user_prompt=prompt if prompt else None,
                params=params,
                require_approval=require_approval,
                thread_id=thread_id,
            )

        clean_result = {}
        for key, value in result.items():
            try:
                json.dumps(value)
                clean_result[key] = value
            except (TypeError, ValueError):
                clean_result[key] = str(value)

        return jsonify(clean_result)
    except Exception as exc:
        app_module.logger.error("LangGraph pipeline failed: %s", type(exc).__name__)
        return redacted_write_error("mapping graph generation", exc)


@bp.route("/api/rollback/<batch_id>", methods=["POST"])
@limiter.limit("3 per minute")
def api_rollback(batch_id):
    app_module = get_app_module()
    body = request.get_json(silent=True) or {}
    approval_error = app_module._approval_error(body, "mapping rollback")
    if approval_error:
        return approval_error
    batch_id = str(batch_id or "").strip()
    if not app_module._valid_version(batch_id):
        return jsonify({"error": "Invalid batch_id."}), 400
    try:
        with app_module.RollbackManager() as rm:
            result = rm.rollback_batch(batch_id)
            return jsonify(redacted_operation_result(result, "mapping rollback"))
    except Exception as exc:
        return redacted_write_error("mapping rollback", exc)
