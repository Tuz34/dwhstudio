from __future__ import annotations

import json
import os

from flask import Blueprint, current_app, jsonify

from ai_egress import get as ai_get
from routes.deps import get_app_module

bp = Blueprint("environment", __name__)
DOWN_UNREACHABLE_STATUS = "Down (Unavailable)"


@bp.route("/api/environment")
def api_environment():
    """Return active environment metadata without exposing sensitive DSN values."""
    app_module = get_app_module()

    return jsonify({
        "environment": app_module.ENVIRONMENT,
        "oracle_dsn": app_module.ORACLE_DSN_DISPLAY,
    })


@bp.route("/api/project_hub")
def api_project_hub():
    """Return project hub metadata with lightweight status checks."""
    app_module = get_app_module()

    try:
        if not os.path.exists("project_hub.json"):
            return jsonify({"error": "Project hub data not found."}), 404

        with open("project_hub.json", "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["architecture"] = [
            item
            for item in data.get("architecture", [])
            if item.get("id") not in ("git", "gitea")
        ]

        is_oracle_up = app_module.test_connection()

        is_ollama_up = False
        try:
            ollama_res = ai_get(app_module.config.OLLAMA_TAGS_URL, timeout=1.5)
            is_ollama_up = ollama_res.status_code == 200
        except Exception:
            is_ollama_up = False

        for item in data.get("architecture", []):
            if item["id"] == "oracle":
                item["status"] = "Active" if is_oracle_up else DOWN_UNREACHABLE_STATUS
            elif item["id"] == "ollama" or item["id"] == "docker":
                item["status"] = "Active" if is_ollama_up else DOWN_UNREACHABLE_STATUS

        return jsonify(data)
    except Exception as exc:
        error_type = type(exc).__name__
        current_app.logger.error("Project hub status check failed: %s", error_type)
        return jsonify({"error": "Project hub status check failed.", "error_type": error_type}), 500
