from __future__ import annotations

from flask import Blueprint, jsonify


bp = Blueprint("disabled_integrations", __name__)


def _git_disabled_response():
    return jsonify({
        "error": "Git/Gitea is disabled.",
        "reason": "Git/Gitea backup is not used because of the risk of leaking passwords and API keys.",
    }), 410


@bp.route("/api/git/status")
def api_git_status():
    return _git_disabled_response()


@bp.route("/api/git/log")
def api_git_log():
    return _git_disabled_response()
