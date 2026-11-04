"""Connectionless absent-only mapping-builder contract for ODI-68."""

from __future__ import annotations

from collections.abc import Mapping
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
)

APPROVAL_SCOPE = "odi68_dim_product_odi_scd2_mapping_creation"


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_absent_only_odi_scd2_mapping_builder_contract",
        "status": status,
        "source": dict(SOURCE),
        "target": dict(TARGET),
        "mapping": MAPPING_NAME,
        "scenario": {"name": SCENARIO_NAME, "version": SCENARIO_VERSION},
        "required_target_ikm": TARGET_IKM,
        "required_lkm": LKM,
        "expected_scd_roles": {
            role: list(columns) for role, columns in EXPECTED_SCD_ROLES.items()
        },
        "approval_scope": APPROVAL_SCOPE,
        "existing_generic_builder_reusable": False,
        "absent_only_mapping_creation": True,
        "mapping_replacement_allowed": False,
        "scenario_generation_included": False,
        "scenario_run_included": False,
        "server_side_executor_remains_baseline": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_dim_product_odi_scd2_mapping_builder_plan() -> dict[str, Any]:
    """Freeze the future builder boundary without implementing a writer."""
    return {
        **_base("blocked_pending_capability_and_contract_review"),
        "required_next": [
            "independently_review_capability_probe",
            "independently_review_mapping_builder_contract",
            "implement_dedicated_absent_only_mapping_builder",
            "independently_review_mapping_builder_implementation",
            "request_fresh_exact_mapping_creation_approval",
        ],
    }


def _packaging_ready(report: Mapping[str, Any]) -> bool:
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


def build_dim_product_odi_scd2_mapping_builder_request(
    *,
    packaging_readiness: Mapping[str, Any],
    capability_probe_review_status: object,
    mapping_builder_contract_review_status: object,
    approved: object,
    approval_scope: object,
) -> dict[str, Any]:
    """Apply future pre-connection gates while deliberately remaining executor-less."""
    if not _packaging_ready(packaging_readiness):
        return _base("blocked_until_packaging_readiness")
    if capability_probe_review_status != "approved":
        return _base("blocked_until_capability_probe_review")
    if mapping_builder_contract_review_status != "approved":
        return _base("blocked_until_mapping_builder_contract_review")
    if approved is not True:
        return _base("blocked_until_explicit_approval")
    if approval_scope != APPROVAL_SCOPE:
        return _base("blocked_until_exact_approval_scope")
    return _base("blocked_pending_separate_mapping_builder_implementation")

