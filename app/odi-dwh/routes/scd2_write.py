from __future__ import annotations

from flask import Blueprint, jsonify, request

import scd2_service
from odi_orchestrator import groovy_approval_context
from routes.deps import get_app_module
from routes.error_responses import redacted_write_error

bp = Blueprint("scd2_write", __name__)
limiter = get_app_module().limiter


def _json_object() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@bp.route("/api/scd2/plan", methods=["POST"])
@limiter.limit("20 per minute")
def api_scd2_plan():
    """Connectionless Type 2 preview; no approval, no writes."""
    body = _json_object()
    try:
        payload, status = scd2_service.plan_preview(body, scd2_service.default_services())
        return jsonify(payload), status
    except (OSError, RuntimeError, ValueError) as exc:
        return redacted_write_error("SCD2 plan", exc)


@bp.route("/api/scd2/generate", methods=["POST"])
@limiter.limit("10 per minute")
def api_scd2_generate():
    """Approved: target DDL, ODI datastore, SCD mapping and scenario."""
    app_module = get_app_module()
    body = _json_object()
    approval_error = app_module._approval_error(body, "SCD2 generation")
    if approval_error:
        return approval_error
    try:
        with groovy_approval_context(body.get("approved")):
            payload, status = scd2_service.generate(
                body, scd2_service.default_services(), approved=body.get("approved")
            )
        return jsonify(payload), status
    except (OSError, RuntimeError, ValueError) as exc:
        return redacted_write_error("SCD2 generation", exc)


@bp.route("/api/scd2/run", methods=["POST"])
@limiter.limit("5 per minute")
def api_scd2_run():
    """Approved: run the generated SCD2 scenario, return count-only postflight."""
    app_module = get_app_module()
    body = _json_object()
    approval_error = app_module._approval_error(body, "SCD2 scenario run")
    if approval_error:
        return approval_error
    try:
        with groovy_approval_context(body.get("approved")):
            payload, status = scd2_service.run(
                body, scd2_service.default_services(), approved=body.get("approved")
            )
        return jsonify(payload), status
    except (OSError, RuntimeError, ValueError) as exc:
        return redacted_write_error("SCD2 scenario run", exc)


@bp.route("/api/scd2/postflight")
@limiter.limit("30 per minute")
def api_scd2_postflight():
    """Count-only evidence for an existing SCD2 target."""
    try:
        payload, status = scd2_service.postflight(request.args, scd2_service.default_services())
        return jsonify(payload), status
    except (OSError, RuntimeError, ValueError) as exc:
        return redacted_write_error("SCD2 postflight", exc)
