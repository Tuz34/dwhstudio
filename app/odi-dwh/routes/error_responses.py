from __future__ import annotations

from flask import current_app, jsonify

from datamart_contract import DATAMART_REQUIRES_TYPE2_CODE, DATAMART_TYPE2_ONLY_WARNING
from skip_sentences import EXPECTED_SKIP_SENTENCES, STEP_SKIPPED


_FAILURE_STATUSES = {"BLOCKED", "ERROR", "FAILED", "FAILURE", "INVALID"}
_AUDIT_PRESERVATION_REJECTED = "audit_preservation_rejected"
_AUDIT_PRESERVATION_SENTENCE = (
    "The target table's audit column types are not supported for audit preservation. "
    "Nothing was created."
)


def redacted_error(action: str, exc: BaseException, *, status: int = 500):
    error_type = type(exc).__name__
    current_app.logger.error("%s failed: %s", action, error_type)
    return jsonify({"error": f"{action} failed.", "error_type": error_type}), status


def redacted_write_error(action: str, exc: BaseException):
    return redacted_error(action, exc)


def _visible_skip_detail(item) -> str:
    text = str(item).strip()
    if text in EXPECTED_SKIP_SENTENCES:
        return text
    return STEP_SKIPPED


def _status_token(value: dict, key: str) -> str:
    raw = value.get(key)
    if raw is None:
        return ""
    return str(raw).strip().upper()


def _redact_skip_value(item):
    if isinstance(item, str):
        return _visible_skip_detail(item)
    if isinstance(item, list):
        return [_redact_skip_value(part) for part in item]
    if isinstance(item, dict):
        return {key: _redact_skip_value(part) for key, part in item.items()}
    return item


def redacted_operation_result(value, action: str):
    """Remove diagnostic exception text from otherwise successful JSON responses."""

    if isinstance(value, list):
        return [redacted_operation_result(item, action) for item in value]
    if not isinstance(value, dict):
        return value

    status = _status_token(value, "status")
    overall = _status_token(value, "overall_status")
    failed = status in _FAILURE_STATUSES or overall in _FAILURE_STATUSES
    skipped = (
        not failed
        and (
            status == "SKIPPED"
            or overall == "SKIPPED"
            or value.get("skipped") is True
        )
    )
    had_error = any(str(key).lower() in {"error", "error_message"} and item for key, item in value.items())
    code = value.get("error_code")
    fixed_error = (
        _AUDIT_PRESERVATION_SENTENCE if code == _AUDIT_PRESERVATION_REJECTED else
        DATAMART_TYPE2_ONLY_WARNING if code == DATAMART_REQUIRES_TYPE2_CODE else None
    )
    cleaned = {}
    for key, item in value.items():
        normalized_key = str(key).lower()
        if normalized_key in {"error", "error_message"} and item:
            cleaned[key] = fixed_error if fixed_error is not None else f"{action} failed."
        elif normalized_key in {"reason", "detail"} and item and failed:
            cleaned[key] = f"{action} failed."
        elif (
            normalized_key in {"reason", "detail", "message"}
            and item
            and skipped
            and isinstance(item, (str, list, dict))
        ):
            cleaned[key] = _redact_skip_value(item)
        else:
            cleaned[key] = redacted_operation_result(item, action)
    if had_error:
        cleaned["error_type"] = "OperationError"
    return cleaned
