from __future__ import annotations

from flask import current_app, jsonify


_FAILURE_STATUSES = {"BLOCKED", "ERROR", "FAILED", "FAILURE", "INVALID", "SKIPPED"}


def redacted_error(action: str, exc: BaseException, *, status: int = 500):
    error_type = type(exc).__name__
    current_app.logger.error("%s failed: %s", action, error_type)
    return jsonify({"error": f"{action} failed.", "error_type": error_type}), status


def redacted_write_error(action: str, exc: BaseException):
    return redacted_error(action, exc)


def redacted_operation_result(value, action: str):
    """Remove diagnostic exception text from otherwise successful JSON responses."""

    if isinstance(value, list):
        return [redacted_operation_result(item, action) for item in value]
    if not isinstance(value, dict):
        return value

    status = str(value.get("status") or value.get("overall_status") or "").upper()
    failed = status in _FAILURE_STATUSES
    had_error = any(str(key).lower() in {"error", "error_message"} and item for key, item in value.items())
    cleaned = {}
    for key, item in value.items():
        normalized_key = str(key).lower()
        if normalized_key in {"error", "error_message"} and item:
            cleaned[key] = f"{action} failed."
        elif normalized_key == "reason" and failed and item:
            cleaned[key] = f"{action} failed."
        elif normalized_key == "detail" and failed and item:
            cleaned[key] = f"{action} failed."
        else:
            cleaned[key] = redacted_operation_result(item, action)
    if had_error:
        cleaned["error_type"] = "OperationError"
    return cleaned
