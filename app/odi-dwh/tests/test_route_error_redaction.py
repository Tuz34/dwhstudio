import ast
import logging
from pathlib import Path

from flask import Flask
from werkzeug.exceptions import InternalServerError, NotFound

from routes.error_responses import redacted_error, redacted_operation_result
from exceptions import DatabaseConnectionError, DatabaseQueryError, DWHBaseError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_redacted_error_returns_fixed_text_and_exception_class_only(caplog):
    app = Flask(__name__)
    secret_detail = "internal credential detail must not leak"

    with caplog.at_level(logging.ERROR), app.app_context():
        response, status = redacted_error("Schema read", RuntimeError(secret_detail))

    body = response.get_json()
    assert status == 500
    assert body == {"error": "Schema read failed.", "error_type": "RuntimeError"}
    assert secret_detail not in response.get_data(as_text=True)
    assert secret_detail not in caplog.text
    assert "RuntimeError" in caplog.text


def test_route_error_logs_do_not_serialize_exception_messages_or_tracebacks():
    unsafe_exception_names = {"exc", "e", "error", "inner_exc", "retry_exc"}

    for path in (PROJECT_ROOT / "routes").glob("*.py"):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"error", "warning", "exception"}:
                continue

            direct_exception_args = [
                arg.id
                for arg in node.args[1:]
                if isinstance(arg, ast.Name) and arg.id in unsafe_exception_names
            ]
            traceback_logging = any(
                keyword.arg == "exc_info"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )

            assert node.func.attr != "exception", f"{path.name}:{node.lineno}"
            assert not direct_exception_args, f"{path.name}:{node.lineno}"
            assert not traceback_logging, f"{path.name}:{node.lineno}"


def test_route_modules_do_not_return_raw_exception_text():
    route_files = (
        "ops_read.py",
        "schema_read.py",
        "schema_write.py",
    )

    for filename in route_files:
        content = (PROJECT_ROOT / "routes" / filename).read_text(encoding="utf-8")
        assert 'jsonify({"error": str(exc)' not in content


def test_operation_result_redacts_http_200_diagnostics_but_keeps_success_output():
    secret_detail = "internal credential detail must not leak"
    result = {
        "overall_status": "FAILED",
        "error": secret_detail,
        "steps": {
            "snapshot": {"status": "SKIPPED", "reason": secret_detail},
            "operator": {"status": "SUCCESS", "odi_result": "Scenario ready"},
        },
        "log": [{"status": "FAILED", "detail": secret_detail}],
    }

    cleaned = redacted_operation_result(result, "mapping update")

    assert cleaned["error"] == "mapping update failed."
    assert cleaned["error_type"] == "OperationError"
    assert cleaned["steps"]["snapshot"]["reason"] == "mapping update failed."
    assert cleaned["steps"]["operator"]["odi_result"] == "Scenario ready"
    assert secret_detail not in str(cleaned)


def test_operation_result_redacts_case_insensitive_persisted_error_message():
    cleaned = redacted_operation_result(
        {"STATUS": "FAILED", "ERROR_MESSAGE": "internal credential detail must not leak"},
        "mapping log read",
    )

    assert cleaned["ERROR_MESSAGE"] == "mapping log read failed."
    assert cleaned["error_type"] == "OperationError"


def test_central_database_error_handlers_never_return_exception_messages():
    import app as app_module

    secret_detail = "internal credential detail must not leak"
    cases = (
        (app_module.handle_db_connection, DatabaseConnectionError(secret_detail), 503, "Database connection failed."),
        (app_module.handle_db_query, DatabaseQueryError(secret_detail), 500, "Database query failed."),
        (app_module.handle_dwh_base, DWHBaseError(secret_detail), 500, "Application operation failed."),
    )

    with app_module.app.app_context():
        for handler, error, expected_status, expected_message in cases:
            response, status = handler(error)
            body = response.get_json()
            assert status == expected_status
            assert body["error"] == expected_message
            assert body["error_type"] == type(error).__name__
            assert secret_detail not in response.get_data(as_text=True)


def test_catch_all_distinguishes_api_not_found_from_internal_server_errors():
    import app as app_module

    with app_module.app.test_request_context("/api/missing"):
        missing_response, missing_status = app_module.catch_all(NotFound())
    with app_module.app.test_request_context("/api/crash"):
        error_response, error_status = app_module.catch_all(InternalServerError())

    assert missing_status == 404
    assert missing_response.get_json() == {"error": "Endpoint not found."}
    assert error_status == 500
    assert error_response.get_json() == {
        "error": "Internal server error.",
        "error_type": "InternalServerError",
    }


def test_catch_all_does_not_render_spa_for_non_api_server_errors():
    import app as app_module

    with app_module.app.test_request_context("/workspace"):
        response, status = app_module.catch_all(InternalServerError())

    assert status == 500
    assert response == "Internal server error."
