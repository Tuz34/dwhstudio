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


_STATUS_NAMES = frozenset({"status", "overall_status", "skipped"})


def _named_values(value: dict, name: str) -> list:
    target = name.lower()
    return [item for key, item in value.items() if str(key).lower() == target]


def _status_tokens(value: dict, name: str) -> list[str]:
    tokens = []
    for raw in _named_values(value, name):
        if raw is None:
            tokens.append("")
        else:
            tokens.append(str(raw).strip().upper())
    return tokens


def _has_own_status(value: dict) -> bool:
    return any(str(key).lower() in _STATUS_NAMES for key in value)


def _failure_and_skip(value: dict) -> tuple[bool, bool]:
    """Any failure spelling wins, then any skip spelling. Letter case does not matter."""
    labels = _status_tokens(value, "status") + _status_tokens(value, "overall_status")
    failed = any(label in _FAILURE_STATUSES for label in labels)
    skipped = not failed and (
        any(label == "SKIPPED" for label in labels)
        or any(item is True for item in _named_values(value, "skipped"))
    )
    return failed, skipped


def _redact_skip_value(item):
    if isinstance(item, str):
        return _visible_skip_detail(item)
    if isinstance(item, list):
        return [_redact_skip_value(part) for part in item]
    if isinstance(item, dict):
        return {key: _redact_skip_value(part) for key, part in item.items()}
    return item


def redacted_operation_result(value, action: str, *, inherited_skip: bool = False):
    """Remove diagnostic exception text from otherwise successful JSON responses.

    A skipped dict lends that skip to every nested dict that has no status of
    its own, at any depth and under any key. A nested dict with its own status
    is judged only by its own values.
    """

    if isinstance(value, list):
        return [
            redacted_operation_result(item, action, inherited_skip=inherited_skip)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    if _has_own_status(value):
        failed, skipped = _failure_and_skip(value)
    elif inherited_skip:
        failed, skipped = False, True
    else:
        failed, skipped = False, False
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
            cleaned[key] = redacted_operation_result(item, action, inherited_skip=skipped)
    if had_error:
        cleaned["error_type"] = "OperationError"
    return cleaned
