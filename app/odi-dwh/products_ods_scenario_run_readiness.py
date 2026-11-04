"""Connectionless run-readiness contract for the ODI-68 PRODUCTS scenario."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from products_ods_scenario_readiness import (
    MAPPING_NAME,
    SCENARIO_NAME,
    SCENARIO_VERSION,
    products_ods_mapping_contract_matches,
)

SCENARIO_CONTEXT = "GLOBAL"
SCENARIO_RUN_APPROVAL_SCOPE = "odi68_products_ods_scenario_run_and_rerun_proof"


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_products_ods_scenario_run_readiness",
        "status": status,
        "mapping": MAPPING_NAME,
        "scenario": {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "context": SCENARIO_CONTEXT,
        },
        "requires_exact_operator_approval": True,
        "required_approval_scope": SCENARIO_RUN_APPROVAL_SCOPE,
        "requires_named_backup": True,
        "requires_separate_run_executor_review": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _exact_scenario_found(report: Mapping[str, Any]) -> bool:
    scenario = report.get("scenario")
    checks = report.get("checks")
    if (
        report.get("status") != "passed"
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
        statuses.get("scenario_found") == "pass"
        and statuses.get("forbidden_operations_not_called") == "pass"
    )


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _candidate_contract_matches(report: Mapping[str, Any]) -> bool:
    fields = {
        name: _count(report.get(name))
        for name in (
            "source_count",
            "target_count",
            "source_duplicate_key_group_count",
            "target_duplicate_key_group_count",
            "insert_candidate_count",
            "update_candidate_count",
            "unchanged_count",
        )
    }
    if (
        report.get("status") != "ready_for_type1_backup"
        or report.get("raw_rows_included") is not False
        or report.get("pk_values_included") is not False
        or any(value is None for value in fields.values())
        or fields["source_duplicate_key_group_count"] != 0
        or fields["target_duplicate_key_group_count"] != 0
    ):
        return False
    return (
        fields["insert_candidate_count"]
        + fields["update_candidate_count"]
        + fields["unchanged_count"]
        == fields["source_count"]
    )


def _backup_contract_matches(
    backup: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    return (
        backup.get("status") == "passed"
        and backup.get("backup_exists") is True
        and _count(backup.get("backup_column_count")) == 24
        and _count(backup.get("backup_row_count")) == _count(candidate.get("target_count"))
        and backup.get("restore_proof_exists_after_cleanup") is False
        and backup.get("raw_rows_included") is False
        and backup.get("pk_values_included") is False
    )


def build_products_ods_scenario_run_readiness(
    mapping_inspection: Mapping[str, Any],
    scenario_probe: Mapping[str, Any],
    candidate_preflight: Mapping[str, Any],
    backup_proof: Mapping[str, Any],
    *,
    scenario_generation_review_status: str,
    approved: object,
    approval_scope: str,
) -> dict[str, Any]:
    """Evaluate run prerequisites without opening a connection or running ODI."""
    if scenario_generation_review_status != "approved":
        return _base("blocked_scenario_generation_review")
    if not products_ods_mapping_contract_matches(mapping_inspection):
        return _base("blocked_mapping_contract")
    if not _exact_scenario_found(scenario_probe):
        return _base("blocked_scenario_probe")
    if not _candidate_contract_matches(candidate_preflight):
        return _base("blocked_candidate_preflight")
    if not _backup_contract_matches(backup_proof, candidate_preflight):
        return _base("blocked_named_backup_proof")
    if approved is not True:
        return _base("blocked_until_exact_operator_approval")
    if approval_scope != SCENARIO_RUN_APPROVAL_SCOPE:
        return _base("blocked_approval_scope")
    return {
        **_base("ready_for_separate_scenario_run_executor"),
        "approved": True,
        "mapping_contract_verified": True,
        "scenario_exact_version_verified": True,
        "candidate_counts_verified": True,
        "named_backup_verified": True,
        "required_next": [
            "independently_review_products_scenario_run_executor",
            "run_exact_scenario_once",
            "run_count_only_postflight",
            "run_exact_scenario_again",
            "prove_second_run_is_noop_with_count_only_postflight",
        ],
    }
