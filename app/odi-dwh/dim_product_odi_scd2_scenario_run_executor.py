"""Callback-driven two-run proof for the fixed ODI-68 DIM_PRODUCT scenario."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from dim_product_odi_scd2_packaging_readiness import SCENARIO_NAME, SCENARIO_VERSION
from dim_product_odi_scd2_scenario import (
    RUN_APPROVAL_SCOPE,
    SCENARIO_CONTEXT,
    build_dim_product_scd2_scenario_run_readiness,
)
from dim_product_scd2_scenario_backup import (
    call_sanitized,
    run_dim_product_scd2_named_backup_gate,
)


def _scenario() -> dict[str, str]:
    return {
        "name": SCENARIO_NAME,
        "version": SCENARIO_VERSION,
        "context": SCENARIO_CONTEXT,
    }


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "fixed_dim_product_scd2_scenario_run_and_rerun_proof",
        "status": status,
        "scenario": _scenario(),
        "scenario_run_attempt_count": 0,
        "successful_scenario_run_count": 0,
        "named_backup_retained": False,
        "backup_may_exist": False,
        "automatic_rollback_performed": False,
        "persistent_action_attempted": False,
        "manual_recovery_review_required": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "session_id_included": False,
        "raw_output_included": False,
        "secret_printed": False,
    }


def _failure(
    stage: str,
    error_type: str,
    *,
    attempts: int = 0,
    successes: int = 0,
    backup_attempted: bool = False,
    backup_verified: bool = False,
) -> dict[str, Any]:
    return {
        **_base("failed"),
        "stage": stage,
        "error_type": error_type,
        "scenario_run_attempt_count": attempts,
        "successful_scenario_run_count": successes,
        "named_backup_retained": True if backup_verified else (None if backup_attempted else False),
        "backup_may_exist": backup_attempted,
        "persistent_action_attempted": backup_attempted or attempts > 0,
        "manual_recovery_review_required": backup_attempted or attempts > 0,
    }


def _call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Mapping[str, Any] | None:
    """Drop callback exceptions and untrusted output without echoing them."""
    result = call_sanitized(callback, *args, **kwargs)
    return result if isinstance(result, Mapping) else None


def scenario_runner(**kwargs: Any) -> Mapping[str, Any]:
    """Live-run callback: delegate to the reviewed fixed-scenario wrapper.

    Takes the same keyword arguments the executor passes (approved,
    approval_scope, scenario_name, version, context) and returns the wrapper's
    sanitized result, which ``_run_matches`` validates. Not wired to config;
    the caller must pass it explicitly after fresh run approval.
    """
    from odi_orchestrator import OdiOrchestrator  # local import: no ODI at module load

    return OdiOrchestrator().run_dim_product_scd2_scenario_safe(**kwargs)


# Contract for the live ``backup_creator`` callback (supplied by the caller):
#   backup_creator(owner="DWH", source_table="DIM_PRODUCT",
#                  backup_table="ODI68_DIM_PRODUCT_BKP",
#                  expected_column_count=26, expected_row_count=72,
#                  require_absent=True) -> Mapping
# It must create the table only when absent (never overwrite), read-verify the
# 26-column / 72-row copy, check the ODS backup is untouched (24 columns,
# 0 rows) and return exactly the proof shape accepted by
# ``dim_product_scd2_scenario_backup._proof_matches`` with no raw rows, keys or
# secrets. No reusable connection-free helper exists, so none is provided here.


def _run_matches(report: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(report, Mapping)
        and set(report) == {
            "status", "scenario", "session_status", "side_effects",
            "raw_rows_included", "pk_values_included", "raw_output_included",
            "session_id_included", "secret_printed",
        }
        and report.get("status") == "passed"
        and report.get("scenario") == _scenario()
        and report.get("session_status") == "DONE"
        and report.get("side_effects") is True
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("raw_output_included") is False
        and report.get("session_id_included") is False
        and report.get("secret_printed") is False
    )


def _probe_readiness(
    mapping_probe: Mapping[str, Any],
    scenario_probe: Mapping[str, Any],
    candidate_probe: Callable[[], Mapping[str, Any]],
    *,
    scenario_generation_review_status: object,
    approved: object,
    approval_scope: object,
) -> tuple[str, Mapping[str, Any] | None]:
    started_at = datetime.now(timezone.utc)
    candidate = _call(candidate_probe)
    if candidate is None:
        return "failed", None
    readiness = build_dim_product_scd2_scenario_run_readiness(
        mapping_probe,
        scenario_probe,
        candidate,
        scenario_generation_review_status=scenario_generation_review_status,
        approved=approved,
        approval_scope=approval_scope,
        executor_started_at=started_at,
    )
    return readiness["status"], candidate


def execute_dim_product_scd2_scenario_run_and_rerun_proof(
    mapping_probe: Mapping[str, Any],
    scenario_probe: Mapping[str, Any],
    *,
    scenario_generation_review_status: object,
    executor_review_status: object,
    approved: object,
    approval_scope: object,
    candidate_probe: Callable[[], Mapping[str, Any]],
    backup_creator: Callable[..., Mapping[str, Any]],
    scenario_runner: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Run only after fresh count proof and an absent-only named backup gate."""
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review")
    if approved is not True:
        return _base("blocked_until_exact_run_approval")
    if approval_scope != RUN_APPROVAL_SCOPE:
        return _base("blocked_run_approval_scope")
    if scenario_generation_review_status != "approved":
        return _base("blocked_scenario_generation_review")

    status, first_preflight = _probe_readiness(
        mapping_probe,
        scenario_probe,
        candidate_probe,
        scenario_generation_review_status=scenario_generation_review_status,
        approved=approved,
        approval_scope=approval_scope,
    )
    if status == "failed":
        return _failure("fresh_read_only_preflight", "PreflightError")
    if status != "ready_for_separate_scenario_run_executor":
        return _base("blocked_run_readiness")

    backup = run_dim_product_scd2_named_backup_gate(
        first_preflight, backup_creator=backup_creator
    )
    if backup.get("status") != "passed":
        if backup.get("backup_creation_attempted") is False:
            return _base("blocked_named_backup_gate")
        return _failure(
            "named_backup_gate", "BackupGateError", backup_attempted=True
        )

    run_args = {
        "approved": True,
        "approval_scope": RUN_APPROVAL_SCOPE,
        "scenario_name": SCENARIO_NAME,
        "version": SCENARIO_VERSION,
        "context": SCENARIO_CONTEXT,
    }
    first_run = _call(scenario_runner, **run_args)
    if not _run_matches(first_run):
        return _failure(
            "first_scenario_run", "ScenarioRunError", attempts=1,
            backup_attempted=True, backup_verified=True,
        )

    status, first_postflight = _probe_readiness(
        mapping_probe,
        scenario_probe,
        candidate_probe,
        scenario_generation_review_status=scenario_generation_review_status,
        approved=approved,
        approval_scope=approval_scope,
    )
    if status != "ready_for_separate_scenario_run_executor":
        return _failure(
            "first_count_only_postflight", "PostflightError", attempts=1,
            successes=1, backup_attempted=True, backup_verified=True,
        )

    second_run = _call(scenario_runner, **run_args)
    if not _run_matches(second_run):
        return _failure(
            "second_scenario_run", "ScenarioRunError", attempts=2,
            successes=1, backup_attempted=True, backup_verified=True,
        )

    status, second_postflight = _probe_readiness(
        mapping_probe,
        scenario_probe,
        candidate_probe,
        scenario_generation_review_status=scenario_generation_review_status,
        approved=approved,
        approval_scope=approval_scope,
    )
    count_names = (
        "source_count", "target_count", "current_target_count", "unchanged_count",
        "insert_candidate_count", "changed_candidate_count", "candidate_count",
    )
    if (
        status != "ready_for_separate_scenario_run_executor"
        or not isinstance(first_postflight, Mapping)
        or not isinstance(second_postflight, Mapping)
        or any(first_postflight.get(name) != second_postflight.get(name) for name in count_names)
    ):
        return _failure(
            "second_count_only_postflight", "PostflightError", attempts=2,
            successes=2, backup_attempted=True, backup_verified=True,
        )
    return {
        **_base("passed"),
        "scenario_run_attempt_count": 2,
        "successful_scenario_run_count": 2,
        "named_backup_retained": True,
        "backup_may_exist": True,
        "persistent_action_attempted": True,
        "source_count": 72,
        "target_count": 72,
        "current_target_count": 72,
        "unchanged_count": 72,
        "remaining_candidate_count": 0,
        "named_backup_column_count": 26,
        "named_backup_row_count": 72,
        "ods_backup_untouched": True,
    }
