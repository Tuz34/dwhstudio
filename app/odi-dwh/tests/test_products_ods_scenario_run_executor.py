from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _readiness():
    return {
        "status": "ready_for_separate_scenario_run_executor",
        "approved": True,
        "required_approval_scope": "odi68_products_ods_scenario_run_and_rerun_proof",
        "mapping_contract_verified": True,
        "scenario_exact_version_verified": True,
        "candidate_counts_verified": True,
        "named_backup_verified": True,
        "scenario": {
            "name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
            "version": "001",
            "context": "GLOBAL",
        },
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def _run_result(status="passed"):
    return {
        "status": status,
        "scenario": {
            "name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
            "version": "001",
            "context": "GLOBAL",
        },
        "session_status": "DONE" if status == "passed" else "ERROR",
        "raw_output_included": False,
        "session_id_included": False,
    }


def _postflight(*, insert=0, update=0, unchanged=72, target=72):
    return {
        "status": "ready_for_type1_backup",
        "source_count": 72,
        "target_count": target,
        "source_duplicate_key_group_count": 0,
        "target_duplicate_key_group_count": 0,
        "insert_candidate_count": insert,
        "update_candidate_count": update,
        "unchanged_count": unchanged,
        "raw_rows_included": False,
        "pk_values_included": False,
    }


class _Runner:
    def __init__(self, results=None):
        self.results = list(results or [_run_result(), _run_result()])
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


class _Probe:
    def __init__(self, results=None):
        self.results = list(results or [_postflight(), _postflight()])
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.results.pop(0)


def _execute(*, readiness=None, review="approved", runner=None, probe=None):
    from products_ods_scenario_run_executor import (
        execute_products_ods_scenario_run_and_rerun_proof,
    )

    runner = runner or _Runner()
    probe = probe or _Probe()
    result = execute_products_ods_scenario_run_and_rerun_proof(
        readiness or _readiness(),
        executor_review_status=review,
        scenario_runner=runner,
        candidate_probe=probe,
    )
    return result, runner, probe


def test_executor_blocks_unreviewed_slice_before_callbacks():
    result, runner, probe = _execute(review="pending")

    assert result["status"] == "blocked_pending_executor_review"
    assert runner.calls == []
    assert probe.calls == 0


def test_executor_revalidates_readiness_before_callbacks():
    readiness = _readiness()
    readiness["required_approval_scope"] = "odi68_products_ods_scenario_generation"

    result, runner, probe = _execute(readiness=readiness)

    assert result["status"] == "blocked_run_readiness"
    assert runner.calls == []
    assert probe.calls == 0


def test_executor_runs_exact_scenario_twice_and_proves_count_only_noop():
    result, runner, probe = _execute()

    assert result["status"] == "passed"
    assert result["scenario_run_attempt_count"] == 2
    assert result["successful_scenario_run_count"] == 2
    assert result["source_count"] == 72
    assert result["target_count"] == 72
    assert result["remaining_insert_candidate_count"] == 0
    assert result["remaining_update_candidate_count"] == 0
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert probe.calls == 2
    assert runner.calls == [
        {
            "approved": True,
            "approval_scope": "odi68_products_ods_scenario_run_and_rerun_proof",
            "scenario_name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
            "version": "001",
            "context": "GLOBAL",
        },
        {
            "approved": True,
            "approval_scope": "odi68_products_ods_scenario_run_and_rerun_proof",
            "scenario_name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
            "version": "001",
            "context": "GLOBAL",
        },
    ]


def test_executor_stops_after_failed_first_run_and_keeps_backup():
    runner = _Runner([_run_result("failed")])
    result, runner, probe = _execute(runner=runner)

    assert result["status"] == "failed"
    assert result["stage"] == "first_scenario_run"
    assert result["scenario_run_attempt_count"] == 1
    assert result["successful_scenario_run_count"] == 0
    assert result["named_backup_retained"] is True
    assert result["automatic_rollback_performed"] is False
    assert result["manual_recovery_review_required"] is True
    assert probe.calls == 0


def test_executor_stops_before_rerun_when_first_postflight_is_not_clean():
    probe = _Probe([_postflight(insert=1, unchanged=71)])
    result, runner, probe = _execute(probe=probe)

    assert result["status"] == "failed"
    assert result["stage"] == "first_count_only_postflight"
    assert result["scenario_run_attempt_count"] == 1
    assert len(runner.calls) == 1
    assert probe.calls == 1


def test_executor_stops_after_failed_second_run_and_keeps_backup():
    runner = _Runner([_run_result(), _run_result("failed")])
    result, runner, probe = _execute(runner=runner)

    assert result["status"] == "failed"
    assert result["stage"] == "second_scenario_run"
    assert result["scenario_run_attempt_count"] == 2
    assert result["successful_scenario_run_count"] == 1
    assert result["named_backup_retained"] is True
    assert probe.calls == 1


def test_executor_rejects_dirty_second_postflight():
    probe = _Probe([_postflight(), _postflight(update=1, unchanged=71)])
    result, _runner, probe = _execute(probe=probe)

    assert result["status"] == "failed"
    assert result["stage"] == "second_count_only_postflight"
    assert result["scenario_run_attempt_count"] == 2
    assert probe.calls == 2


def test_executor_rejects_runtime_output_or_session_identifier_surface():
    unsafe = _run_result()
    unsafe["session_id_included"] = True
    runner = _Runner([unsafe])

    result, _runner, probe = _execute(runner=runner)

    assert result["status"] == "failed"
    assert result["stage"] == "first_scenario_run"
    assert probe.calls == 0


def test_executor_has_no_endpoint_config_or_direct_runtime_import():
    text = (PROJECT_ROOT / "products_ods_scenario_run_executor.py").read_text(encoding="utf-8")
    lowered = text.lower()

    assert "@app.route" not in lowered
    assert "blueprint" not in lowered
    assert "import config" not in lowered
    assert "odi_orchestrator" not in lowered
    assert "oracledb" not in lowered
    assert '"raw_rows_included": false' in lowered
    assert '"pk_values_included": false' in lowered


def test_executor_runbook_documents_fail_closed_commit_boundary():
    text = (PROJECT_ROOT / "docs" / "dwh-products-ods-scenario-run-executor.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()

    for required in (
        "exact scenario twice",
        "count-only postflight",
        "named backup remains",
        "no automatic rollback",
        "separate exact approval",
        "no live scenario run was performed",
        "no raw rows",
        "no pk values",
    ):
        assert required in lowered
