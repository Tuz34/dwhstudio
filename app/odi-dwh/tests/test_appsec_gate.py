from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if not (WORKSPACE_ROOT / "tools" / "workspace" / "appsec_gate.py").is_file():
    pytest.skip("Repository AppSec gate is absent from this application-only tree.", allow_module_level=True)
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.workspace import appsec_gate


ACTIVE_APP_ROOT = WORKSPACE_ROOT / "app" / "odi-dwh"


@pytest.fixture
def workspace_tmp(request):
    path = WORKSPACE_ROOT / ".tmp_appsec_tests" / request.node.name
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        for file_path in path.glob("*"):
            if file_path.is_file():
                file_path.write_text("# sanitized temporary test artifact\n", encoding="utf-8")


def test_endpoint_matrix_marks_mapping_run_as_approval_gated_and_rate_limited():
    matrix = appsec_gate.build_endpoint_matrix(ACTIVE_APP_ROOT / "app.py")

    mapping_run = next(
        row
        for row in matrix
        if row["path"] == "/api/mapping/run" and "POST" in row["methods"]
    )
    assert mapping_run["approval_required"] is True
    assert mapping_run["rate_limit"] == "5 per minute"
    assert mapping_run["risk"] == "persistent_write"

    status = next(row for row in matrix if row["path"] == "/api/status")
    assert status["methods"] == ["GET"]
    assert status["risk"] == "read"
    assert status["approval_required"] is False


def test_endpoint_matrix_marks_incremental_execute_as_approval_gated_and_rate_limited():
    matrix = appsec_gate.build_endpoint_matrix(ACTIVE_APP_ROOT / "app.py")

    incremental_execute = next(
        row
        for row in matrix
        if row["path"] == "/api/incremental/execute" and "POST" in row["methods"]
    )
    assert incremental_execute["approval_required"] is True
    assert incremental_execute["rate_limit"] == "5 per minute"
    assert incremental_execute["risk"] == "persistent_write"


def test_endpoint_matrix_marks_quality_and_skey_persistent_actions_as_critical_writes():
    matrix = appsec_gate.build_endpoint_matrix(ACTIVE_APP_ROOT / "app.py")
    expected_limits = {
        "/api/quality/autofix-column": "10 per minute",
        "/api/quality/autofix-all-columns": "5 per minute",
        "/api/quality/sync-mapping": "5 per minute",
        "/api/quality/create-error-table": "5 per minute",
        "/api/quality/create-all-error-tables": "3 per minute",
        "/api/quality/validate-insert": "10 per minute",
        "/api/skey/create": "5 per minute",
        "/api/mapping/snapshot": "10 per minute",
        "/api/rollback/<batch_id>": "3 per minute",
        "/api/lineage/refresh": "3 per minute",
        "/api/schema-changes/detect": "5 per minute",
        "/api/alerts/acknowledge": "10 per minute",
        "/api/alerts/check": "5 per minute",
        "/api/rag_knowledge": "10 per minute",
    }

    for path, rate_limit in expected_limits.items():
        row = next(item for item in matrix if item["path"] == path and "POST" in item["methods"])
        assert row["risk"] == "persistent_write", path
        assert row["approval_required"] is True, path
        assert row["rate_limit"] == rate_limit, path


def test_gate_fails_when_quality_persistent_action_lacks_approval_and_rate_limit(workspace_tmp):
    app_py = workspace_tmp / "app.py"
    app_py.write_text(
        "\n".join(
            [
                "from flask import Flask",
                "app = Flask(__name__)",
                "@app.route('/api/quality/sync-mapping', methods=['POST'])",
                "def sync_mapping():",
                "    return {'ok': True}",
            ]
        ),
        encoding="utf-8",
    )
    config_py = workspace_tmp / "config.py"
    config_py.write_text(
        'APP_HOST = os.getenv("APP_HOST", "127.0.0.1")\n'
        'APP_DEBUG = os.getenv("APP_DEBUG", "false")\n',
        encoding="utf-8",
    )

    report = appsec_gate.run_gate(app_py=app_py, config_py=config_py, scan_root=workspace_tmp)

    assert report["passed"] is False
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["critical_writes_require_approval"]["passed"] is False
    assert checks["critical_writes_are_rate_limited"]["passed"] is False


def test_endpoint_matrix_includes_routes_moved_to_blueprint_files(workspace_tmp):
    app_py = workspace_tmp / "app.py"
    app_py.write_text(
        "\n".join(
            [
                "from flask import Flask",
                "app = Flask(__name__)",
                "",
                "def _approval_error():",
                "    return {'error': 'approval required'}",
            ]
        ),
        encoding="utf-8",
    )
    blueprint_py = workspace_tmp / "routes" / "incremental.py"
    blueprint_py.parent.mkdir(parents=True, exist_ok=True)
    blueprint_py.write_text(
        "\n".join(
            [
                "from flask import Blueprint",
                "bp = Blueprint('incremental', __name__)",
                "",
                "@limiter.limit('5 per minute')",
                "@bp.route('/api/incremental/execute', methods=['POST'])",
                "def execute_incremental():",
                "    return _approval_error()",
            ]
        ),
        encoding="utf-8",
    )

    matrix = appsec_gate.build_endpoint_matrix(app_py)

    incremental_execute = next(
        row
        for row in matrix
        if row["path"] == "/api/incremental/execute" and "POST" in row["methods"]
    )
    assert incremental_execute["approval_required"] is True
    assert incremental_execute["rate_limit"] == "5 per minute"
    assert incremental_execute["risk"] == "persistent_write"


def test_endpoint_matrix_infers_approval_from_delegated_app_function(workspace_tmp):
    app_py = workspace_tmp / "app.py"
    app_py.write_text(
        "\n".join(
            [
                "def api_incremental_execute():",
                "    body = request.get_json() or {}",
                "    approval_error = _approval_error(body, 'incremental append')",
                "    if approval_error:",
                "        return approval_error",
                "    return {'ok': True}",
            ]
        ),
        encoding="utf-8",
    )
    blueprint_py = workspace_tmp / "routes" / "incremental.py"
    blueprint_py.parent.mkdir(parents=True, exist_ok=True)
    blueprint_py.write_text(
        "\n".join(
            [
                "from flask import Blueprint",
                "bp = Blueprint('incremental', __name__)",
                "",
                "@limiter.limit('5 per minute')",
                "@bp.route('/api/incremental/execute', methods=['POST'])",
                "def api_incremental_execute():",
                "    return app_module.api_incremental_execute()",
            ]
        ),
        encoding="utf-8",
    )

    matrix = appsec_gate.build_endpoint_matrix(app_py)

    incremental_execute = next(
        row
        for row in matrix
        if row["path"] == "/api/incremental/execute" and "POST" in row["methods"]
    )
    assert incremental_execute["approval_required"] is True
    assert incremental_execute["rate_limit"] == "5 per minute"
    assert incremental_execute["risk"] == "persistent_write"


def test_endpoint_matrix_infers_approval_from_delegated_helper_call(workspace_tmp):
    app_py = workspace_tmp / "app.py"
    app_py.write_text(
        "\n".join(
            [
                "def _schema_change_apply_db_payload(body):",
                "    if body.get('approved') is not True:",
                "        raise ValueError('approved:true is required')",
                "    return {'ok': True}",
                "",
                "def api_schema_change_apply_db():",
                "    return _schema_change_apply_db_payload(request.get_json() or {})",
            ]
        ),
        encoding="utf-8",
    )
    blueprint_py = workspace_tmp / "routes" / "schema_write.py"
    blueprint_py.parent.mkdir(parents=True, exist_ok=True)
    blueprint_py.write_text(
        "\n".join(
            [
                "from flask import Blueprint",
                "bp = Blueprint('schema_write', __name__)",
                "",
                "@limiter.limit('5 per minute')",
                "@bp.route('/api/schema/change/apply-db', methods=['POST'])",
                "def api_schema_change_apply_db():",
                "    return app_module.api_schema_change_apply_db()",
            ]
        ),
        encoding="utf-8",
    )

    matrix = appsec_gate.build_endpoint_matrix(app_py)

    schema_apply = next(
        row
        for row in matrix
        if row["path"] == "/api/schema/change/apply-db" and "POST" in row["methods"]
    )
    assert schema_apply["approval_required"] is True
    assert schema_apply["rate_limit"] == "5 per minute"
    assert schema_apply["risk"] == "persistent_write"


def test_endpoint_matrix_infers_approval_from_renamed_delegated_app_function(workspace_tmp):
    app_py = workspace_tmp / "app.py"
    app_py.write_text(
        "\n".join(
            [
                "def _schema_change_apply_db_payload(body):",
                "    if body.get('approved') is not True:",
                "        raise ValueError('approved:true is required')",
                "    return {'ok': True}",
                "",
                "def api_schema_change_apply_db():",
                "    return _schema_change_apply_db_payload(request.get_json() or {})",
            ]
        ),
        encoding="utf-8",
    )
    blueprint_py = workspace_tmp / "routes" / "schema_write.py"
    blueprint_py.parent.mkdir(parents=True, exist_ok=True)
    blueprint_py.write_text(
        "\n".join(
            [
                "from flask import Blueprint",
                "bp = Blueprint('schema_write', __name__)",
                "",
                "@limiter.limit('5 per minute')",
                "@bp.route('/api/schema/change/apply-db', methods=['POST'])",
                "def schema_apply_wrapper():",
                "    return app_module.api_schema_change_apply_db()",
            ]
        ),
        encoding="utf-8",
    )

    matrix = appsec_gate.build_endpoint_matrix(app_py)

    schema_apply = next(
        row
        for row in matrix
        if row["path"] == "/api/schema/change/apply-db" and "POST" in row["methods"]
    )
    assert schema_apply["approval_required"] is True
    assert schema_apply["rate_limit"] == "5 per minute"
    assert schema_apply["risk"] == "persistent_write"


def test_gate_fails_when_csrf_exempt_is_present(workspace_tmp):
    app_py = workspace_tmp / "app.py"
    app_py.write_text(
        "\n".join(
            [
                "from flask import Flask",
                "app = Flask(__name__)",
                "@app.route('/api/example', methods=['POST'])",
                "@csrf.exempt",
                "def example():",
                "    return 'ok'",
            ]
        ),
        encoding="utf-8",
    )
    config_py = workspace_tmp / "config.py"
    config_py.write_text(
        'APP_HOST = os.getenv("APP_HOST", "127.0.0.1")\n'
        'APP_DEBUG = os.getenv("APP_DEBUG", "false")\n',
        encoding="utf-8",
    )

    report = appsec_gate.run_gate(app_py=app_py, config_py=config_py, scan_root=workspace_tmp)

    assert report["passed"] is False
    csrf_check = next(check for check in report["checks"] if check["id"] == "no_csrf_exempt")
    assert csrf_check["passed"] is False


def test_secret_scan_reports_redacted_findings(workspace_tmp):
    source = workspace_tmp / "unsafe.py"
    source.write_text("PASSWORD = 'fake-secret-for-redaction-test'\n", encoding="utf-8")

    findings = appsec_gate.scan_secret_patterns(workspace_tmp)

    assert findings
    payload = json.dumps(findings)
    assert "fake-secret-for-redaction-test" not in payload
    assert "<redacted>" in payload
