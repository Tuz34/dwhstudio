"""Trusted server map from an ODI context and physical owner to one model code.

The value is process configuration. It is not a client preference, and parsing it
does not open a repository or prove that the model exists.
"""

from __future__ import annotations

import json
import re

SOURCE_MODEL_CONFIG_INVALID = "source_model_config_invalid"
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


class SourceModelConfigError(Exception):
    """The configured map is not one strict context-owner-model object."""

    code = SOURCE_MODEL_CONFIG_INVALID

    def __init__(self) -> None:
        super().__init__(self.code)


def _pairs(pairs: list[tuple[object, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    seen: set[str] = set()
    for key, value in pairs:
        if not isinstance(key, str) or key in seen:
            raise SourceModelConfigError
        seen.add(key)
        parsed[key] = value
    return parsed


def parse_source_model_bindings(raw: object) -> dict[str, dict[str, str]]:
    """Accept only a JSON object of context to owner to model code."""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        raise SourceModelConfigError
    try:
        parsed = json.loads(raw, object_pairs_hook=_pairs)
    except SourceModelConfigError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise SourceModelConfigError from None
    if type(parsed) is not dict:
        raise SourceModelConfigError
    bindings: dict[str, dict[str, str]] = {}
    for context, owners in parsed.items():
        if not _IDENTIFIER.fullmatch(context) or type(owners) is not dict:
            raise SourceModelConfigError
        owner_map: dict[str, str] = {}
        for owner, model_code in owners.items():
            if (
                not isinstance(owner, str)
                or not isinstance(model_code, str)
                or not _IDENTIFIER.fullmatch(owner)
                or not _IDENTIFIER.fullmatch(model_code)
            ):
                raise SourceModelConfigError
            owner_map[owner] = model_code
        bindings[context] = owner_map
    return bindings


def trusted_source_model_code(context: object, owner: object, raw: object) -> str | None:
    """Return the configured model, or None when this request has no binding.

    Invalid configuration raises. A request with no owner, or no matching pair,
    does not select a model and does not fall back to another entry.
    """
    bindings = parse_source_model_bindings(raw)
    if owner is None or not isinstance(context, str) or not isinstance(owner, str):
        return None
    if not _IDENTIFIER.fullmatch(context) or not _IDENTIFIER.fullmatch(owner):
        return None
    return bindings.get(context, {}).get(owner)
