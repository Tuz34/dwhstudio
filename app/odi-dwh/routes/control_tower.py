from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify


bp = Blueprint("control_tower", __name__)


@bp.route("/api/control-tower/evidence")
def api_control_tower_evidence():
    """Return redacted local dev/TST evidence without live ODI or DB side effects."""
    from routes.deps import get_app_module
    app_module = get_app_module()

    return jsonify(app_module.build_control_tower_evidence(Path(app_module.app.root_path)))
