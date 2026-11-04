from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    """Register route blueprints after app.py has defined shared helpers."""
    from routes.ai_tools import bp as ai_tools_bp
    from routes.ai_write import bp as ai_write_bp
    from routes.auth_write import bp as auth_write_bp
    from routes.control_tower import bp as control_tower_bp
    from routes.disabled_integrations import bp as disabled_integrations_bp
    from routes.environment import bp as environment_bp
    from routes.mapping_write import bp as mapping_write_bp
    from routes.misc_write import bp as misc_write_bp
    from routes.ops_read import bp as ops_read_bp
    from routes.public_ui import bp as public_ui_bp
    from routes.quality_write import bp as quality_write_bp
    from routes.schema_read import bp as schema_read_bp
    from routes.schema_write import bp as schema_write_bp
    from routes.security_status import bp as security_status_bp
    from routes.status import bp as status_bp

    app.register_blueprint(ai_tools_bp)
    app.register_blueprint(ai_write_bp)
    app.register_blueprint(auth_write_bp)
    app.register_blueprint(control_tower_bp)
    app.register_blueprint(disabled_integrations_bp)
    app.register_blueprint(environment_bp)
    app.register_blueprint(mapping_write_bp)
    app.register_blueprint(misc_write_bp)
    app.register_blueprint(ops_read_bp)
    app.register_blueprint(public_ui_bp)
    app.register_blueprint(quality_write_bp)
    app.register_blueprint(schema_read_bp)
    app.register_blueprint(schema_write_bp)
    app.register_blueprint(security_status_bp)
    app.register_blueprint(status_bp)
