from __future__ import annotations

from flask import Blueprint, jsonify


bp = Blueprint("ai_tools", __name__)


@bp.route("/api/ai/tools", methods=["GET"])
def api_ai_tools():
    """AI-Q orchestrator tool registry contract."""
    from routes.deps import get_app_module
    app_module = get_app_module()

    return jsonify({"ok": True, "tools": app_module.get_tool_registry()})
