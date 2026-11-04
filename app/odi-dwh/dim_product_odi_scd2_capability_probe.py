"""Strict normalization for the ODI-68 DIM_PRODUCT metadata capability probe."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from dim_product_odi_scd2_packaging_readiness import (
    EXPECTED_SCD_ROLES,
    LKM,
    MAPPING_NAME,
    SCENARIO_NAME,
    SCENARIO_VERSION,
    SOURCE,
    TARGET,
    TARGET_IKM,
    build_dim_product_odi_scd2_packaging_readiness,
)

TOOL = "odi.dim_product_scd2_capability_probe"
RAW_MODE = "odi_sdk_finders_reflection_only"
NORMALIZED_MODE = "normalized_odi_sdk_metadata_only"

_SUCCESS_KEYS = {
    "tool",
    "status",
    "mode",
    "side_effects",
    "repository_connection_attempted",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "capability",
    "datastore",
    "objects",
}
_ERROR_KEYS = {
    "tool",
    "status",
    "mode",
    "side_effects",
    "repository_connection_attempted",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "error_type",
}
_CAPABILITY_KEYS = {
    "status",
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "target_technology",
    "target_ikm",
    "target_ikm_installed",
    "target_ikm_assignable",
    "lkm",
    "scd2_role_assignment_supported",
}
_DATASTORE_KEYS = {
    "status",
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "source",
    "target",
    "target_keys",
    "scd_roles",
}
_OBJECT_KEYS = {
    "status",
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "mapping",
    "scenario",
}
_ROLE_KEYS = set(EXPECTED_SCD_ROLES)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")


def _base(status: str, *, repository_connection_attempted: bool) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "status": status,
        "mode": NORMALIZED_MODE,
        "side_effects": False,
        "repository_connection_attempted": repository_connection_attempted,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _invalid(error_type: str = "InvalidProbeOutput") -> dict[str, Any]:
    return {
        **_base(
            "blocked_invalid_probe_output",
            repository_connection_attempted=True,
        ),
        "error": "ODI capability probe output was rejected.",
        "error_type": error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "InvalidProbeOutput",
    }


def _safe_flags(report: Mapping[str, Any]) -> bool:
    return (
        report.get("side_effects") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
    )


def _string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(
            isinstance(item, str) and _SAFE_IDENTIFIER_RE.fullmatch(item)
            for item in value
        )
        and len(value) == len(set(value))
    )


def _optional_bool(value: object) -> bool:
    return value is None or type(value) is bool


def _identity(
    value: object,
    expected: Mapping[str, str],
) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"owner", "table", "column_count"}
        and value.get("owner") == expected["owner"]
        and value.get("table") == expected["table"]
        and isinstance(value.get("column_count"), int)
        and not isinstance(value.get("column_count"), bool)
        and value["column_count"] >= 0
    )


def _valid_capability(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _CAPABILITY_KEYS:
        return False
    return (
        value.get("status") == "passed"
        and _safe_flags(value)
        and value.get("target_technology") in {None, "ORACLE"}
        and value.get("target_ikm") in {None, TARGET_IKM}
        and type(value.get("target_ikm_installed")) is bool
        and type(value.get("target_ikm_assignable")) is bool
        and value.get("lkm") in {None, LKM}
        and type(value.get("scd2_role_assignment_supported")) is bool
    )


def _valid_datastore(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _DATASTORE_KEYS:
        return False
    target_keys = value.get("target_keys")
    roles = value.get("scd_roles")
    if (
        not isinstance(target_keys, Mapping)
        or set(target_keys) != {"primary_key_columns", "natural_key_columns"}
        or not _string_list(target_keys.get("primary_key_columns"))
        or not _string_list(target_keys.get("natural_key_columns"))
        or not isinstance(roles, Mapping)
        or set(roles) != _ROLE_KEYS
        or not all(_string_list(roles.get(key)) for key in _ROLE_KEYS)
    ):
        return False
    allowed_columns = {
        column
        for role_columns in EXPECTED_SCD_ROLES.values()
        for column in role_columns
    }
    observed_columns = {
        column for role_columns in roles.values() for column in role_columns
    }
    key_columns = {
        column for columns in target_keys.values() for column in columns
    }
    return (
        value.get("status") == "passed"
        and _safe_flags(value)
        and _identity(value.get("source"), SOURCE)
        and _identity(value.get("target"), TARGET)
        and observed_columns <= allowed_columns
        and key_columns <= allowed_columns
    )


def _valid_objects(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _OBJECT_KEYS:
        return False
    mapping = value.get("mapping")
    scenario = value.get("scenario")
    return (
        value.get("status") == "attention_required"
        and _safe_flags(value)
        and isinstance(mapping, Mapping)
        and set(mapping) == {"name", "exists"}
        and mapping.get("name") == MAPPING_NAME
        and _optional_bool(mapping.get("exists"))
        and isinstance(scenario, Mapping)
        and set(scenario) == {"name", "version", "exists"}
        and scenario.get("name") == SCENARIO_NAME
        and scenario.get("version") == SCENARIO_VERSION
        and _optional_bool(scenario.get("exists"))
    )


def _valid_root_flags(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("tool") == TOOL
        and payload.get("mode") == RAW_MODE
        and _safe_flags(payload)
        and payload.get("repository_connection_attempted") is True
    )


def normalize_dim_product_odi_scd2_capability_probe(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact metadata-only JSON and return a redacted normalized result."""
    try:
        if not isinstance(payload, Mapping):
            return _invalid()
        if payload.get("status") == "error":
            error_type = payload.get("error_type")
            if (
                set(payload) != _ERROR_KEYS
                or not _valid_root_flags(payload)
                or not isinstance(error_type, str)
                or not _ERROR_TYPE_RE.fullmatch(error_type)
            ):
                return _invalid()
            return {
                **_base("error", repository_connection_attempted=True),
                "error": "ODI capability probe failed.",
                "error_type": error_type,
            }
        if (
            set(payload) != _SUCCESS_KEYS
            or payload.get("status") != "passed"
            or not _valid_root_flags(payload)
            or not _valid_capability(payload.get("capability"))
            or not _valid_datastore(payload.get("datastore"))
            or not _valid_objects(payload.get("objects"))
        ):
            return _invalid()

        capability = deepcopy(payload["capability"])
        datastore = deepcopy(payload["datastore"])
        objects = deepcopy(payload["objects"])
        readiness = build_dim_product_odi_scd2_packaging_readiness(
            capability,
            datastore,
            objects,
        )
        return {
            **_base("passed", repository_connection_attempted=True),
            "capability": capability,
            "datastore": datastore,
            "objects": objects,
            "readiness": readiness,
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid(type(exc).__name__)
