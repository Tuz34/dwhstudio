"""Review-gated orchestration contract for two PRODUCTS ODI scenario runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from products_ods_scenario_readiness import SCENARIO_NAME, SCENARIO_VERSION
from products_ods_scenario_run_readiness import (
    SCENARIO_CONTEXT,
    SCENARIO_RUN_APPROVAL_SCOPE,
)


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "products_ods_scenario_run_and_rerun_proof",
        "status": status,
        "scenario": {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "context": SCENARIO_CONTEXT,
        },
        "will_execute": False,
        "scenario_run_attempt_count": 0,
        "successful_scenario_run_count": 0,
        "named_backup_retained": True,
        "automatic_rollback_performed": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "session_id_included": False,
        "secret_printed": False,
    }


def _readiness_matches(readiness: Mapping[str, Any]) -> bool:
    return (
        readiness.get("status") == "ready_for_separate_scenario_run_executor"
        and readiness.get("approved") is True
        and readiness.get("required_approval_scope") == SCENARIO_RUN_APPROVAL_SCOPE
        and readiness.get("mapping_contract_verified") is True
        and readiness.get("scenario_exact_version_verified") is True
        and readiness.get("candidate_counts_verified") is True
        and readiness.get("named_backup_verified") is True
        and readiness.get("scenario")
        == {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "context": SCENARIO_CONTEXT,
        }
        and readiness.get("raw_rows_included") is False
        and readiness.get("pk_values_included") is False
    )


def _run_result_matches(result: Mapping[str, Any]) -> bool:
    return (
        result.get("status") == "passed"
        and result.get("outcome_unknown", False) is False
        and result.get("session_status") == "DONE"
        and result.get("scenario")
        == {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "context": SCENARIO_CONTEXT,
        }
        and result.get("raw_output_included") is False
        and result.get("session_id_included") is False
    )


def _count(report: Mapping[str, Any], name: str) -> int | None:
    value = report.get(name)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _clean_postflight(report: Mapping[str, Any]) -> bool:
    source_count = _count(report, "source_count")
    target_count = _count(report, "target_count")
    unchanged_count = _count(report, "unchanged_count")
    return (
        report.get("status") == "ready_for_type1_backup"
        and source_count is not None
        and target_count == source_count
        and unchanged_count == source_count
        and _count(report, "source_duplicate_key_group_count") == 0
        and _count(report, "target_duplicate_key_group_count") == 0
        and _count(report, "insert_candidate_count") == 0
        and _count(report, "update_candidate_count") == 0
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
    )


def _failure(
    stage: str,
    *,
    attempts: int,
    successes: int,
    outcome_unknown: bool = False,
) -> dict[str, Any]:
    return {
        **_base("unknown" if outcome_unknown else "failed"),
        **({"outcome_unknown": True} if outcome_unknown else {}),
        "stage": stage,
        "will_execute": attempts > 0,
        "scenario_run_attempt_count": attempts,
        "successful_scenario_run_count": successes,
        "error_type": "ScenarioOutcomeUnknown" if outcome_unknown else "ScenarioRunVerificationError",
        "manual_recovery_review_required": attempts > 0,
    }


def execute_products_ods_scenario_run_and_rerun_proof(
    run_readiness: Mapping[str, Any],
    *,
    executor_review_status: str,
    scenario_runner: Callable[..., Mapping[str, Any]],
    candidate_probe: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run the fixed scenario twice only after reviewed, exact readiness."""
    if not _readiness_matches(run_readiness):
        return _base("blocked_run_readiness")
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review")

    run_args = {
        "approved": True,
        "approval_scope": SCENARIO_RUN_APPROVAL_SCOPE,
        "scenario_name": SCENARIO_NAME,
        "version": SCENARIO_VERSION,
        "context": SCENARIO_CONTEXT,
    }
    first_run = scenario_runner(**run_args)
    if not _run_result_matches(first_run):
        return _failure(
            "first_scenario_run", attempts=1, successes=0,
            outcome_unknown=first_run.get("status") == "unknown" or first_run.get("outcome_unknown") is True,
        )

    first_postflight = candidate_probe()
    if not _clean_postflight(first_postflight):
        return _failure("first_count_only_postflight", attempts=1, successes=1)

    second_run = scenario_runner(**run_args)
    if not _run_result_matches(second_run):
        return _failure(
            "second_scenario_run", attempts=2, successes=1,
            outcome_unknown=second_run.get("status") == "unknown" or second_run.get("outcome_unknown") is True,
        )

    second_postflight = candidate_probe()
    if not _clean_postflight(second_postflight):
        return _failure("second_count_only_postflight", attempts=2, successes=2)
    if _count(second_postflight, "target_count") != _count(first_postflight, "target_count"):
        return _failure("rerun_target_count_drift", attempts=2, successes=2)

    return {
        **_base("passed"),
        "will_execute": True,
        "scenario_run_attempt_count": 2,
        "successful_scenario_run_count": 2,
        "source_count": _count(second_postflight, "source_count"),
        "target_count": _count(second_postflight, "target_count"),
        "remaining_insert_candidate_count": _count(
            second_postflight,
            "insert_candidate_count",
        ),
        "remaining_update_candidate_count": _count(
            second_postflight,
            "update_candidate_count",
        ),
        "unchanged_count": _count(second_postflight, "unchanged_count"),
        "manual_recovery_review_required": False,
    }
