from __future__ import annotations

from flask import Blueprint, jsonify


bp = Blueprint("security_status", __name__)


@bp.route("/api/auth/status")
def api_auth_status():
    from routes.deps import get_app_module
    app_module = get_app_module()

    user = app_module._current_user()
    return jsonify({
        "enabled": app_module._auth_enabled(),
        "configured": app_module._auth_configured(),
        "authenticated": bool(user),
        "user": user,
        "roles": sorted(app_module._ROLE_ORDER, key=app_module._ROLE_ORDER.get),
    })


@bp.route("/api/deployment/security-profile")
def api_deployment_security_profile():
    from routes.deps import get_app_module
    app_module = get_app_module()

    return jsonify(app_module._DEPLOYMENT_SECURITY_PROFILE)
