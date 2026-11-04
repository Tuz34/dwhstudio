"""Connectionless readiness contract for the ODI-68 PRODUCTS scenario."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAPPING_NAME = "MAP_SH_PRODUCTS_TO_ODS"
SCENARIO_NAME = "MAP_SH_PRODUCTS_TO_ODS_SCN"
SCENARIO_VERSION = "001"
APPROVAL_SCOPE = "odi68_products_ods_scenario_generation"


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_products_ods_scenario_readiness",
        "status": status,
        "mapping": MAPPING_NAME,
        "scenario": {"name": SCENARIO_NAME, "version": SCENARIO_VERSION},
        "requires_independent_mapping_review": True,
        "requires_exact_operator_approval": True,
        "required_approval_scope": APPROVAL_SCOPE,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def products_ods_mapping_contract_matches(report: Mapping[str, Any]) -> bool:
    """Return whether a read-only report matches the reviewed PRODUCTS mapping."""
    physical = report.get("physical")
    logical = report.get("logical")
    if report.get("status") != "passed" or report.get("side_effects") is not False:
        return False
    if not isinstance(physical, Mapping) or not isinstance(logical, Mapping):
        return False
    if physical.get("target_ikm") != "IKM Oracle Merge":
        return False
    if physical.get("lkm") != "LKM SQL to Oracle (Built-In)":
        return False
    if logical.get("integration_type") != "INCREMENTAL_UPDATE":
        return False
    if logical.get("update_key_columns") != ["PROD_ID"]:
        return False
    audit = logical.get("audit_expressions")
    return isinstance(audit, Mapping) and audit == {
        "ODS_CREATED_AT": "CURRENT_TIMESTAMP",
        "ODS_UPDATED_AT": "CURRENT_TIMESTAMP",
    }


def _scenario_is_absent(report: Mapping[str, Any]) -> bool:
    scenario = report.get("scenario")
    checks = report.get("checks")
    if (
        report.get("status") != "attention_required"
        or report.get("side_effects") is not False
        or not isinstance(scenario, Mapping)
        or scenario.get("name") != SCENARIO_NAME
        or scenario.get("version") != SCENARIO_VERSION
        or not isinstance(checks, list)
    ):
        return False
    statuses = {
        str(item.get("name")): item.get("status")
        for item in checks
        if isinstance(item, Mapping)
    }
    return (
        statuses.get("scenario_found") == "fail"
        and statuses.get("forbidden_operations_not_called") == "pass"
    )


def build_products_ods_scenario_readiness(
    mapping_inspection: Mapping[str, Any],
    scenario_probe: Mapping[str, Any],
    *,
    mapping_review_status: str,
    approved: object,
    approval_scope: str,
) -> dict[str, Any]:
    """Evaluate static gates without opening a connection or writing metadata."""
    if mapping_review_status != "approved":
        return _base("blocked_mapping_review")
    if not products_ods_mapping_contract_matches(mapping_inspection):
        return _base("blocked_mapping_contract")
    if not _scenario_is_absent(scenario_probe):
        if scenario_probe.get("status") == "passed":
            return _base("blocked_scenario_already_exists")
        return _base("blocked_scenario_probe")
    if approved is not True:
        return _base("blocked_until_explicit_approval")
    if approval_scope != APPROVAL_SCOPE:
        return _base("blocked_approval_scope")
    return {
        **_base("ready_for_absent_only_scenario_generation"),
        "approved": True,
        "mapping_contract_verified": True,
        "scenario_exact_version_absent": True,
        "required_next": [
            "invoke_reviewed_absent_only_generator_after_exact_approval",
            "run_read_only_scenario_probe_after_generation",
            "do_not_run_scenario_without_separate_approval",
        ],
    }
