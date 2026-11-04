"""Connectionless scenario generation and run-readiness gates for ODI-68."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from dim_product_odi_scd2_packaging_readiness import (
    MAPPING_NAME,
    SCENARIO_NAME,
    SCENARIO_VERSION,
    TARGET_IKM,
)

SCENARIO_CONTEXT = "GLOBAL"
GENERATION_APPROVAL_SCOPE = "odi68_dim_product_odi_scd2_scenario_generation"
RUN_APPROVAL_SCOPE = "odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof"
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")


def _base(status: str, *, scope: str) -> dict[str, Any]:
    return {
        "mode": "static_dim_product_odi_scd2_scenario_boundary",
        "status": status,
        "mapping": MAPPING_NAME,
        "scenario": {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "context": SCENARIO_CONTEXT,
        },
        "required_approval_scope": scope,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _mapping_probe_matches(report: Mapping[str, Any]) -> bool:
    mapping = report.get("mapping")
    if (
        report.get("tool") != "odi.dim_product_scd2_mapping_probe"
        or report.get("status") != "passed"
        or report.get("mode") != "normalized_fixed_scd2_mapping_metadata"
        or report.get("side_effects") is not False
        or report.get("repository_connection_attempted") is not True
        or report.get("raw_rows_included") is not False
        or report.get("pk_values_included") is not False
        or report.get("secret_printed") is not False
        or not isinstance(mapping, Mapping)
    ):
        return False
    access_points = mapping.get("access_point_count")
    lkm_assigned = mapping.get("lkm_assigned_count")
    return (
        set(mapping)
        == {
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
        and mapping.get("name") == MAPPING_NAME
        and mapping.get("source_component_count") == 1
        and mapping.get("target_component_count") == 1
        and mapping.get("target_attribute_count") == 26
        and mapping.get("mapped_attribute_count") == 26
        and mapping.get("integration_type") == "SCD"
        and mapping.get("target_ikm") == TARGET_IKM
        and isinstance(access_points, int)
        and not isinstance(access_points, bool)
        and access_points >= 0
        and isinstance(lkm_assigned, int)
        and not isinstance(lkm_assigned, bool)
        and lkm_assigned == access_points
    )


def _scenario_probe_matches(report: Mapping[str, Any], *, exists: bool) -> bool:
    scenario = report.get("scenario")
    return (
        report.get("status") == ("passed" if exists else "attention_required")
        and report.get("side_effects") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and isinstance(scenario, Mapping)
        and set(scenario) == {"name", "version", "exists"}
        and scenario.get("name") == SCENARIO_NAME
        and scenario.get("version") == SCENARIO_VERSION
        and scenario.get("exists") is exists
    )


def build_dim_product_scd2_scenario_generation_readiness(
    mapping_probe: Mapping[str, Any],
    scenario_probe: Mapping[str, Any],
    *,
    mapping_builder_review_status: object,
    approved: object,
    approval_scope: object,
) -> dict[str, Any]:
    """Gate absent-only scenario generation without invoking ODI."""
    if mapping_builder_review_status != "approved":
        return _base("blocked_mapping_builder_review", scope=GENERATION_APPROVAL_SCOPE)
    if not _mapping_probe_matches(mapping_probe):
        return _base("blocked_mapping_probe", scope=GENERATION_APPROVAL_SCOPE)
    if not _scenario_probe_matches(scenario_probe, exists=False):
        return _base("blocked_scenario_probe", scope=GENERATION_APPROVAL_SCOPE)
    if approved is not True:
        return _base("blocked_until_exact_generation_approval", scope=GENERATION_APPROVAL_SCOPE)
    if approval_scope != GENERATION_APPROVAL_SCOPE:
        return _base("blocked_generation_approval_scope", scope=GENERATION_APPROVAL_SCOPE)
    return {
        **_base("ready_for_absent_only_scenario_generation", scope=GENERATION_APPROVAL_SCOPE),
        "approved": True,
        "scenario_generation_included": True,
        "scenario_run_included": False,
        "required_next": [
            "independently_review_scenario_generator_wrapper",
            "generate_exact_absent_scenario_after_fresh_approval",
            "run_fresh_read_only_scenario_probe",
            "review_run_readiness_separately",
        ],
    }


def _invalid_generation(error_type: str = "InvalidGenerationOutput") -> dict[str, Any]:
    safe_type = error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "InvalidGenerationOutput"
    return {
        "tool": "odi.dim_product_scd2_scenario_generation",
        "status": "blocked_invalid_generation_output",
        "mode": "normalized_fixed_absent_only_scenario_generation",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "error": "ODI scenario generation output was rejected.",
        "error_type": safe_type,
    }


def normalize_dim_product_scd2_scenario_generation(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Reduce generic generator output to a fixed status-only contract."""
    try:
        if not isinstance(payload, Mapping):
            return _invalid_generation()
        allowed_root = {
            "tool",
            "status",
            "mode",
            "side_effects",
            "repository_connection_attempted",
            "scenario",
            "checks",
        }
        scenario = payload.get("scenario")
        checks = payload.get("checks")
        if (
            set(payload) != allowed_root
            or payload.get("tool") != "odi.generate_scenario_metadata"
            or payload.get("status") != "passed"
            or payload.get("mode") != "scenario_metadata_generation"
            or payload.get("side_effects") is not True
            or payload.get("repository_connection_attempted") is not True
            or not isinstance(scenario, Mapping)
            or set(scenario) != {"name", "version"}
            or scenario.get("name") != SCENARIO_NAME
            or scenario.get("version") != SCENARIO_VERSION
            or not isinstance(checks, list)
        ):
            return _invalid_generation()
        allowed_check_keys = {"name", "status", "detail", "metadata"}
        valid_checks = [
            item
            for item in checks
            if isinstance(item, Mapping)
            and {"name", "status"} <= set(item) <= allowed_check_keys
            and isinstance(item.get("name"), str)
            and item.get("status") in {"pass", "fail", "warning"}
        ]
        if len(valid_checks) != len(checks):
            return _invalid_generation()
        statuses = {item["name"]: item["status"] for item in valid_checks}
        if (
            statuses.get("scenario_generated") != "pass"
            or statuses.get("forbidden_operations_not_called") != "pass"
        ):
            return _invalid_generation()
        return {
            "tool": "odi.dim_product_scd2_scenario_generation",
            "status": "passed",
            "mode": "normalized_fixed_absent_only_scenario_generation",
            "side_effects": True,
            "repository_connection_attempted": True,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "scenario": {"name": SCENARIO_NAME, "version": SCENARIO_VERSION},
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid_generation(type(exc).__name__)


def _count(report: Mapping[str, Any], name: str) -> int | None:
    value = report.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _noop_candidate_matches(report: Mapping[str, Any]) -> bool:
    count_names = (
        "source_count",
        "target_count",
        "current_target_count",
        "source_duplicate_key_group_count",
        "target_duplicate_surrogate_key_group_count",
        "target_multiple_current_key_group_count",
        "target_invalid_current_flag_count",
        "target_invalid_effective_range_count",
        "source_missing_current_count",
        "insert_candidate_count",
        "changed_candidate_count",
        "unchanged_count",
        "candidate_count",
        "backup_exists",
        "backup_column_count",
        "backup_row_count",
    )
    counts = {name: _count(report, name) for name in count_names}
    if (
        report.get("status") != "rerun_noop"
        or report.get("raw_rows_included") is not False
        or report.get("pk_values_included") is not False
        or report.get("secret_printed") is not False
        or any(value is None for value in counts.values())
    ):
        return False
    zero_fields = (
        "source_duplicate_key_group_count",
        "target_duplicate_surrogate_key_group_count",
        "target_multiple_current_key_group_count",
        "target_invalid_current_flag_count",
        "target_invalid_effective_range_count",
        "source_missing_current_count",
        "insert_candidate_count",
        "changed_candidate_count",
        "candidate_count",
        "backup_row_count",
    )
    return (
        all(counts[name] == 0 for name in zero_fields)
        and counts["source_count"] == counts["target_count"]
        and counts["source_count"] == counts["current_target_count"]
        and counts["source_count"] == counts["unchanged_count"]
        and counts["backup_exists"] == 1
        and counts["backup_column_count"] == 24
    )


def build_dim_product_scd2_scenario_run_readiness(
    mapping_probe: Mapping[str, Any],
    scenario_probe: Mapping[str, Any],
    candidate_preflight: Mapping[str, Any],
    *,
    scenario_generation_review_status: object,
    approved: object,
    approval_scope: object,
) -> dict[str, Any]:
    """Gate a future two-run no-op proof without providing a runner."""
    if scenario_generation_review_status != "approved":
        return _base("blocked_scenario_generation_review", scope=RUN_APPROVAL_SCOPE)
    if not _mapping_probe_matches(mapping_probe):
        return _base("blocked_mapping_probe", scope=RUN_APPROVAL_SCOPE)
    if not _scenario_probe_matches(scenario_probe, exists=True):
        return _base("blocked_scenario_probe", scope=RUN_APPROVAL_SCOPE)
    if not _noop_candidate_matches(candidate_preflight):
        return _base("blocked_candidate_preflight", scope=RUN_APPROVAL_SCOPE)
    if approved is not True:
        return _base("blocked_until_exact_run_approval", scope=RUN_APPROVAL_SCOPE)
    if approval_scope != RUN_APPROVAL_SCOPE:
        return _base("blocked_run_approval_scope", scope=RUN_APPROVAL_SCOPE)
    return {
        **_base("ready_for_separate_scenario_run_executor", scope=RUN_APPROVAL_SCOPE),
        "approved": True,
        "requires_two_noop_runs": True,
        "requires_count_only_postflight_after_each_run": True,
        "requires_separate_run_executor_review": True,
        "named_ods_backup_verified": True,
        "required_next": [
            "implement_fixed_scenario_run_and_rerun_executor",
            "independently_review_scenario_run_executor",
            "request_fresh_exact_run_approval",
        ],
    }
