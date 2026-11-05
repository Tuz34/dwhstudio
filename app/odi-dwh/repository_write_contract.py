"""Value-free, fail-closed repository transaction outcome contracts."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
FLAGS = ("commit_attempted", "commit_acknowledged", "rollback_attempted",
         "rollback_succeeded", "manual_recovery_review_required")
BASE_KEYS = {"tool", "mode", "status", "side_effects", "repository_connection_attempted"}
SAFETY_KEYS = {"raw_rows_included", "pk_values_included", "secret_printed"}
FAILURE_KEYS = BASE_KEYS | SAFETY_KEYS | {"error_type", "repository_outcome", *FLAGS}
STATES = {
    "no_transaction": ("error", False, False, False, False, False, False),
    "rolled_back": ("error", False, False, False, True, True, False),
    "rollback_failed": ("outcome_unknown", None, False, False, True, False, True),
    "commit_outcome_unknown": ("outcome_unknown", None, True, False, True, None, True),
    "committed": ("committed_report_incomplete", True, True, True, False, False, True),
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_json(output: object) -> Any:
    if not isinstance(output, str):
        raise ValueError("Invalid JSON output type")
    return json.loads(output.strip(), object_pairs_hook=_unique_object)


def unverified(error_type: str = "InvalidGroovyOutput") -> dict:
    safe = error_type if isinstance(error_type, str) and ERROR_TYPE.fullmatch(error_type) else "InvalidGroovyOutput"
    return {"side_effects": None, "repository_outcome": "unverified",
            "manual_recovery_review_required": True, "error_type": safe}


def failure(payload: object, tool: str, mode: str, *, generator: bool = False) -> dict | None:
    """Only a validated acknowledgement can establish a clean rollback/no transaction."""
    keys = (FAILURE_KEYS - SAFETY_KEYS) | {"checks"} if generator else FAILURE_KEYS
    if not isinstance(payload, Mapping) or set(payload) != keys:
        return None
    outcome = payload.get("repository_outcome")
    if not isinstance(outcome, str) or outcome not in STATES:
        return None
    status, side_effects, *flags = STATES[outcome]
    if (payload.get("tool") != tool or payload.get("mode") != mode
            or payload.get("status") != status or payload.get("side_effects") is not side_effects
            or payload.get("repository_connection_attempted") is not True
            or not isinstance(payload.get("error_type"), str) or not ERROR_TYPE.fullmatch(payload["error_type"])):
        return None
    if not generator and any(payload.get(key) is not False for key in SAFETY_KEYS):
        return None
    if generator:
        checks = payload.get("checks")
        if not isinstance(checks, list) or any(
            not isinstance(item, Mapping) or not {"name", "status"} <= set(item) <= {"name", "status", "detail", "metadata"}
            or not isinstance(item["name"], str) or item["status"] not in ("pass", "fail", "warning") for item in checks
        ):
            return None
    for key, expected in zip(FLAGS, flags, strict=True):
        if type(payload.get(key)) is not bool or (expected is not None and payload[key] is not expected):
            return None
    return {"repository_outcome": outcome, "side_effects": side_effects, "error_type": payload["error_type"],
            **{key: payload[key] for key in FLAGS}}
