from __future__ import annotations

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash

from routes.deps import get_app_module


bp = Blueprint("auth_write", __name__)
limiter = get_app_module().limiter


@bp.route("/api/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_auth_login():
    app_module = get_app_module()
    if not app_module._auth_enabled():
        return jsonify({"authenticated": False, "enabled": False})
    if not app_module._auth_configured():
        app_module._record_security_event("login_unconfigured", "blocked")
        return jsonify({"error": "Authentication is enabled but not configured."}), 503

    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if username != app_module.APP_AUTH_USERNAME or not check_password_hash(
        app_module.APP_AUTH_PASSWORD_HASH,
        password,
    ):
        session.pop("auth_user", None)
        app_module._record_security_event("login_failed", "blocked", username=username)
        return jsonify({"error": "Invalid username or password."}), 401

    role = (
        app_module.APP_AUTH_DEFAULT_ROLE
        if app_module.APP_AUTH_DEFAULT_ROLE in app_module._ROLE_ORDER
        else "operator"
    )
    session.permanent = True
    session["auth_user"] = {"username": username, "role": role}
    app_module._record_security_event("login_success", "allowed", username=username, role=role)
    return jsonify({"authenticated": True, "user": app_module._current_user()})


@bp.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    app_module = get_app_module()
    old_user = app_module._current_user()
    session.pop("auth_user", None)
    if app_module._auth_enabled():
        app_module._record_security_event("logout", "allowed", username=(old_user or {}).get("username"))
    return jsonify({"authenticated": False})
