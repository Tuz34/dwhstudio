from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, send_from_directory
from flask_wtf.csrf import generate_csrf


bp = Blueprint("public_ui", __name__)


def render_react_index():
    try:
        path = os.path.join(current_app.static_folder, "react", "index.html")
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()

        token = generate_csrf()
        target = '<meta name="csrf-token" content="" />'
        replacement = f'<meta name="csrf-token" content="{token}" />'

        if target in content:
            content = content.replace(target, replacement)
        else:
            content = content.replace("</head>", f'    <meta name="csrf-token" content="{token}" />\n  </head>')

        response = current_app.make_response(content)
        response.set_cookie("csrf_token", token, samesite="Lax")
        return response
    except Exception as exc:
        current_app.logger.error(f"React index load error: {exc}")
        return "Frontend build not found or error. Run 'npm run build' in frontend folder.", 404


@bp.route("/")
def index():
    return render_react_index()


@bp.route("/legacy")
def legacy_index():
    return jsonify({
        "error": "Legacy interface removed.",
        "reason": "React UI is the only supported interface.",
    }), 410


@bp.route("/assets/<path:path>")
def serve_react_assets(path):
    return send_from_directory("static/react/assets", path)
