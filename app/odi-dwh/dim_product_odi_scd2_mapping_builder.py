"""Dedicated absent-only DIM_PRODUCT SCD2 mapping builder boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from dim_product_odi_scd2_mapping_builder_contract import APPROVAL_SCOPE
from dim_product_odi_scd2_packaging_readiness import (
    LKM,
    MAPPING_NAME,
    SCENARIO_NAME,
    SCENARIO_VERSION,
    SOURCE,
    TARGET,
    TARGET_IKM,
)

TOOL = "odi.dim_product_scd2_mapping_create"
RAW_MODE = "fixed_absent_only_scd2_mapping_creation"
NORMALIZED_MODE = "normalized_fixed_absent_only_scd2_mapping_creation"
SCRIPT_NAME = "dim_product_scd2_mapping_create.groovy"
INTEGRATION_TYPE = "SCD"

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
    "result",
}
_ERROR_KEYS = (_ROOT_KEYS - {"result"}) | {"error_type"}
_RESULT_KEYS = {
    "status",
    "mapping_name",
    "source_component_count",
    "target_component_count",
    "target_attribute_count",
    "mapped_attribute_count",
    "integration_type",
    "target_ikm",
    "access_point_count",
    "lkm_assigned_count",
    "commit_completed",
}


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "fixed_absent_only_scd2_mapping_builder",
        "status": status,
        "source": dict(SOURCE),
        "target": dict(TARGET),
        "mapping": MAPPING_NAME,
        "scenario": {"name": SCENARIO_NAME, "version": SCENARIO_VERSION},
        "target_ikm": TARGET_IKM,
        "lkm": LKM,
        "integration_type": INTEGRATION_TYPE,
        "approval_scope": APPROVAL_SCOPE,
        "absent_only": True,
        "replace_existing_mapping": False,
        "scenario_generation_included": False,
        "scenario_run_included": False,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _readiness_matches(report: Mapping[str, Any]) -> bool:
    return (
        report.get("status")
        == "ready_for_separate_mapping_builder_implementation_review"
        and report.get("source") == SOURCE
        and report.get("target") == TARGET
        and report.get("mapping") == MAPPING_NAME
        and report.get("scenario")
        == {"name": SCENARIO_NAME, "version": SCENARIO_VERSION}
        and report.get("required_target_ikm") == TARGET_IKM
        and report.get("required_lkm") == LKM
        and report.get("server_side_executor_remains_baseline") is True
        and report.get("mapping_collision_free") is True
        and report.get("scenario_collision_free") is True
        and report.get("target_ikm_verified") is True
        and report.get("scd2_roles_verified") is True
        and report.get("will_execute") is False
        and report.get("connection_attempted") is False
        and report.get("write_operations_attempted") == []
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
    )


def build_dim_product_odi_scd2_mapping_request(
    *,
    packaging_readiness: Mapping[str, Any],
    mapping_contract_review_status: object,
    mapping_builder_implementation_review_status: object,
    approved: object,
    approval_scope: object,
) -> dict[str, Any]:
    """Apply every gate before the fixed mapping writer is reachable."""
    if not _readiness_matches(packaging_readiness):
        return _base("blocked_packaging_readiness")
    if mapping_contract_review_status != "approved":
        return _base("blocked_mapping_contract_review")
    if mapping_builder_implementation_review_status != "approved":
        return _base("blocked_mapping_builder_review")
    if approved is not True:
        return _base("blocked_until_explicit_approval")
    if approval_scope != APPROVAL_SCOPE:
        return _base("blocked_until_exact_approval_scope")
    return _base("ready_for_dedicated_mapping_builder_execution")


def _normalized_base(status: str, *, side_effects: bool) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "status": status,
        "mode": NORMALIZED_MODE,
        "side_effects": side_effects,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _invalid(error_type: str = "InvalidMappingOutput") -> dict[str, Any]:
    safe_type = error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "InvalidMappingOutput"
    return {
        **_normalized_base("blocked_invalid_mapping_output", side_effects=False),
        "error": "ODI SCD2 mapping output was rejected.",
        "error_type": safe_type,
    }


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def normalize_dim_product_odi_scd2_mapping(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept only fixed identity plus count/status metadata from the writer."""
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
                **_normalized_base("error", side_effects=False),
                "error": "ODI SCD2 mapping creation failed.",
                "error_type": error_type,
            }

        result = payload.get("result")
        if (
            set(payload) != _ROOT_KEYS
            or payload.get("tool") != TOOL
            or payload.get("status") != "passed"
            or payload.get("mode") != RAW_MODE
            or payload.get("side_effects") is not True
            or payload.get("repository_connection_attempted") is not True
            or payload.get("raw_rows_included") is not False
            or payload.get("pk_values_included") is not False
            or payload.get("secret_printed") is not False
            or not isinstance(result, Mapping)
            or set(result) != _RESULT_KEYS
            or result.get("status") != "passed"
            or result.get("mapping_name") != MAPPING_NAME
            or result.get("source_component_count") != 1
            or result.get("target_component_count") != 1
            or result.get("target_attribute_count") != 26
            or result.get("mapped_attribute_count") != 26
            or result.get("integration_type") != INTEGRATION_TYPE
            or result.get("target_ikm") != TARGET_IKM
            or not _non_negative_int(result.get("access_point_count"))
            or not _non_negative_int(result.get("lkm_assigned_count"))
            or result["lkm_assigned_count"] > result["access_point_count"]
            or result.get("commit_completed") is not True
        ):
            return _invalid()
        return {
            **_normalized_base("passed", side_effects=True),
            "result": deepcopy(result),
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid(type(exc).__name__)

