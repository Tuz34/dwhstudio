"""Topology-bound execution identity for SCD2 grants.

The Oracle account comes from one target model, one context, one physical schema
and that schema's data server. A configured name is not proof. Public status
never carries the account, a password, a URL, or a connection property.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TOOL = "odi.execution_identity"
MODE = "topology_bound_execution_identity"
SCRIPT_NAME = "execution_identity_probe.groovy"
EXECUTION_CONTEXT = "GLOBAL"
ORACLE_TECHNOLOGY = "Oracle"
IDENTIFIER = re.compile(r"^[A-Z0-9_$#]{1,30}$")

BLOCKED_CODES = frozenset(
    {
        "execution_identity_invalid",
        "execution_identity_missing",
        "execution_identity_ambiguous",
        "execution_identity_unverified",
        "execution_identity_rejected",
    }
)
_FLAGS = (
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
)
_BLOCKED_KEYS = frozenset(
    {
        "tool",
        "status",
        "code",
        "mode",
        "repository_connection_attempted",
        "model_count",
        "context_count",
        "mapping_count",
        *_FLAGS,
    }
)
_PASSED_KEYS = frozenset(
    {
        "tool",
        "status",
        "mode",
        "repository_connection_attempted",
        "model_count",
        "context_count",
        "mapping_count",
        "data_server_count",
        "model_code",
        "context_code",
        "schema_name",
        "technology",
        "data_server_technology",
        "username",
        *_FLAGS,
    }
)
_COUNT_KEYS = ("model_count", "context_count", "mapping_count")


class ExecutionIdentityError(ValueError):
    """Fail closed with a stable code and no identity text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionIdentityProof:
    """Internal proof. Do not place this object on an API response."""

    username: str
    model_code: str
    context_code: str
    schema_name: str

    def __post_init__(self) -> None:
        for value in (self.username, self.model_code, self.context_code, self.schema_name):
            if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
                raise ExecutionIdentityError("execution_identity_invalid")


def validate_identity_request(
    model_code: object, context_code: object, schema_name: object
) -> tuple[str, str, str] | None:
    """Accept only server-derived identifiers. The context is fixed to GLOBAL."""
    values = (model_code, context_code, schema_name)
    if any(not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None for value in values):
        return None
    if context_code != EXECUTION_CONTEXT:
        return None
    return model_code, context_code, schema_name


def public_blocked(
    code: str, *, connected: bool, counts: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """Redacted diagnostic. Counts are numbers; names are omitted."""
    if code not in BLOCKED_CODES:
        code = "execution_identity_unverified"
    payload: dict[str, Any] = {
        "tool": TOOL,
        "status": "blocked",
        "code": code,
        "mode": MODE,
        "repository_connection_attempted": connected is True,
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    if counts is not None:
        for key in _COUNT_KEYS:
            value = counts.get(key)
            if type(value) is int:
                payload[key] = value
    return payload


def public_passed(
    *, model_code: str, context_code: str, schema_name: str
) -> dict[str, Any]:
    """Passed diagnostic. The account stays on the internal proof only."""
    return {
        "tool": TOOL,
        "status": "passed",
        "mode": MODE,
        "repository_connection_attempted": True,
        "model_code": model_code,
        "context_code": context_code,
        "schema_name": schema_name,
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate")
    return dict(pairs)


def _flag(payload: Mapping[str, Any], name: str, expected: bool) -> bool:
    return payload.get(name) is expected


def _count(value: object) -> int | None:
    if type(value) is not int or value < 0 or value > 1000:
        return None
    return value


def _blocked_status(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if set(payload) != _BLOCKED_KEYS or payload.get("status") != "blocked":
        return None
    if payload.get("tool") != TOOL or payload.get("mode") != MODE:
        return None
    code = payload.get("code")
    if not isinstance(code, str) or code not in BLOCKED_CODES:
        return None
    if type(payload.get("repository_connection_attempted")) is not bool:
        return None
    if any(not _flag(payload, name, False) for name in _FLAGS):
        return None
    counts: dict[str, int] = {}
    for key in _COUNT_KEYS:
        parsed = _count(payload.get(key))
        if parsed is None:
            return None
        counts[key] = parsed
    return public_blocked(
        code, connected=payload["repository_connection_attempted"], counts=counts
    )


def _passed_proof(
    payload: Mapping[str, Any], *, model_code: str, context_code: str, schema_name: str
) -> ExecutionIdentityProof | None:
    if set(payload) != _PASSED_KEYS or payload.get("status") != "passed":
        return None
    if payload.get("tool") != TOOL or payload.get("mode") != MODE:
        return None
    if payload.get("repository_connection_attempted") is not True:
        return None
    if any(not _flag(payload, name, False) for name in _FLAGS):
        return None
    for key in (*_COUNT_KEYS, "data_server_count"):
        if _count(payload.get(key)) != 1:
            return None
    if payload.get("technology") != ORACLE_TECHNOLOGY:
        return None
    if payload.get("data_server_technology") != ORACLE_TECHNOLOGY:
        return None
    if payload.get("model_code") != model_code or payload.get("context_code") != context_code:
        return None
    if payload.get("schema_name") != schema_name:
        return None
    username = payload.get("username")
    if not isinstance(username, str) or IDENTIFIER.fullmatch(username) is None:
        return None
    return ExecutionIdentityProof(username, model_code, context_code, schema_name)


def interpret_execution_identity(
    output: object, *, model_code: str, context_code: str, schema_name: str
) -> tuple[dict[str, Any], ExecutionIdentityProof | None]:
    """Return a redacted status and, only when every check passes, the proof."""
    expected = validate_identity_request(model_code, context_code, schema_name)
    if expected is None:
        return public_blocked("execution_identity_invalid", connected=False), None
    if not isinstance(output, str) or not output.strip().startswith("{"):
        return public_blocked("execution_identity_unverified", connected=True), None
    try:
        payload = json.loads(output.strip(), object_pairs_hook=_pairs)
    except (json.JSONDecodeError, ValueError, TypeError):
        return public_blocked("execution_identity_unverified", connected=True), None
    if not isinstance(payload, dict):
        return public_blocked("execution_identity_unverified", connected=True), None
    blocked = _blocked_status(payload)
    if blocked is not None:
        return blocked, None
    proof = _passed_proof(
        payload,
        model_code=expected[0],
        context_code=expected[1],
        schema_name=expected[2],
    )
    if proof is None:
        return public_blocked("execution_identity_unverified", connected=True), None
    return public_passed(
        model_code=expected[0], context_code=expected[1], schema_name=expected[2]
    ), proof
