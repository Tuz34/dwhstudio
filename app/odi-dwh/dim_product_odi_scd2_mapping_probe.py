"""Strict normalizer for the fixed DIM_PRODUCT SCD2 mapping probe."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from dim_product_odi_scd2_packaging_readiness import MAPPING_NAME, TARGET_IKM

TOOL = "odi.dim_product_scd2_mapping_probe"
RAW_MODE = "fixed_scd2_mapping_metadata_probe"
NORMALIZED_MODE = "normalized_fixed_scd2_mapping_metadata"
SCRIPT_NAME = "dim_product_scd2_mapping_probe.groovy"

_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
_ROOT_KEYS = {
    "tool",
    "status",
    "mode",
    "side_effects",
    "repository_connection_attempted",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "mapping",
}
_ERROR_KEYS = (_ROOT_KEYS - {"mapping"}) | {"error_type"}
_MAPPING_KEYS = {
    "name",
    "source_component_count",
    "target_component_count",
    "target_attribute_count",
    "mapped_attribute_count",
    "integration_type",
    "target_ikm",
    "access_point_count",
    "lkm_assigned_count",
}


def _base(status: str) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "status": status,
        "mode": NORMALIZED_MODE,
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _invalid(error_type: str = "InvalidMappingProbeOutput") -> dict[str, Any]:
    safe_type = (
        error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "InvalidMappingProbeOutput"
    )
    return {
        **_base("blocked_invalid_mapping_probe_output"),
        "error": "ODI SCD2 mapping probe output was rejected.",
        "error_type": safe_type,
    }


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_dim_product_odi_scd2_mapping_probe(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept exact count/status metadata and reject all expression details."""
    try:
        if not isinstance(payload, Mapping):
            return _invalid()
        if payload.get("status") == "error":
            error_type = payload.get("error_type")
            if (
                set(payload) != _ERROR_KEYS
                or payload.get("tool") != TOOL
                or payload.get("mode") != RAW_MODE
                or payload.get("side_effects") is not False
                or payload.get("repository_connection_attempted") is not True
                or payload.get("raw_rows_included") is not False
                or payload.get("pk_values_included") is not False
                or payload.get("secret_printed") is not False
                or not isinstance(error_type, str)
                or not _ERROR_TYPE_RE.fullmatch(error_type)
            ):
                return _invalid()
            return {
                **_base("error"),
                "error": "ODI SCD2 mapping probe failed.",
                "error_type": error_type,
            }

        mapping = payload.get("mapping")
        if (
            set(payload) != _ROOT_KEYS
            or payload.get("tool") != TOOL
            or payload.get("status") != "passed"
            or payload.get("mode") != RAW_MODE
            or payload.get("side_effects") is not False
            or payload.get("repository_connection_attempted") is not True
            or payload.get("raw_rows_included") is not False
            or payload.get("pk_values_included") is not False
            or payload.get("secret_printed") is not False
            or not isinstance(mapping, Mapping)
            or set(mapping) != _MAPPING_KEYS
            or mapping.get("name") != MAPPING_NAME
            or _count(mapping.get("source_component_count")) != 1
            or _count(mapping.get("target_component_count")) != 1
            or _count(mapping.get("target_attribute_count")) != 26
            or _count(mapping.get("mapped_attribute_count")) != 26
            or mapping.get("integration_type") != "SCD"
            or mapping.get("target_ikm") != TARGET_IKM
        ):
            return _invalid()
        access_points = _count(mapping.get("access_point_count"))
        assigned_lkms = _count(mapping.get("lkm_assigned_count"))
        if access_points is None or assigned_lkms != access_points:
            return _invalid()
        return {**_base("passed"), "mapping": deepcopy(mapping)}
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid(type(exc).__name__)

