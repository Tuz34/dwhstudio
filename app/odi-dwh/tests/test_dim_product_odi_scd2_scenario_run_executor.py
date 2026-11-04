"""Mocked execution contract for the fixed DIM_PRODUCT scenario run."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest


SCOPE = "odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof"
SCENARIO = {
    "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
    "version": "001",
    "context": "GLOBAL",
}


def _mapping_probe():
    return {
        "tool": "odi.dim_product_scd2_mapping_probe",
        "status": "passed",
        "mode": "normalized_fixed_scd2_mapping_metadata",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "mapping": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
            "source_component_count": 1,
            "target_component_count": 1,
            "target_attribute_count": 26,
            "mapped_attribute_count": 26,
            "integration_type": "SCD",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "access_point_count": 0,
            "lkm_assigned_count": 0,
        },
    }


def _scenario_probe():
    return {
        "status": "passed",
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "scenario": {
            "name": SCENARIO["name"],
            "version": "001",
            "exists": True,
        },
    }


def _preflight():
    return {
        "status": "rerun_noop",
        "connection_attempted": True,
        "write_attempted": False,
        "observed_at": "NOW",
        "source_count": 72,
        "target_count": 72,
        "current_target_count": 72,
        "source_duplicate_key_group_count": 0,
        "target_duplicate_surrogate_key_group_count": 0,
        "target_multiple_current_key_group_count": 0,
        "target_invalid_current_flag_count": 0,
        "target_invalid_effective_range_count": 0,
        "source_missing_current_count": 0,
        "insert_candidate_count": 0,
        "changed_candidate_count": 0,
        "unchanged_count": 72,
        "candidate_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _backup_result():
    return {
        "status": "passed",
        "side_effects": True,
        "backup": {
            "owner": "DWH",
            "table": "ODI68_DIM_PRODUCT_BKP",
            "source_table": "DIM_PRODUCT",
            "exists": True,
            "created_absent_only": True,
            "column_count": 26,
            "row_count": 72,
        },
        "ods_backup": {
            "owner": "DWH",
            "table": "ODI68_ODS_PRODUCTS_BKP",
            "exists": True,
            "column_count": 24,
            "row_count": 0,
            "untouched": True,
        },
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _run_result():
    return {
        "status": "passed",
        "scenario": dict(SCENARIO),
        "session_status": "DONE",
        "side_effects": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "raw_output_included": False,
        "session_id_included": False,
        "secret_printed": False,
    }


def _execute(*, mapping=None, scenario=None, review="approved", approved=True,
             scope=SCOPE, preflights=None, backup=None, runs=None, retained_backup=False):
    from dim_product_odi_scd2_scenario_run_executor import (
        execute_dim_product_scd2_scenario_run_and_rerun_proof,
    )

    events = []
    probe_calls = 0
    evidence = list(preflights) if preflights is not None else [
        _preflight(), _preflight(), _preflight()
    ]
    run_evidence = list(runs) if runs is not None else [_run_result(), _run_result()]

    def candidate_probe():
        nonlocal probe_calls
        events.append("preflight" if probe_calls == 0 else "postflight")
        probe_calls += 1
        value = evidence.pop(0)
        if isinstance(value, Exception):
            raise value
        value = deepcopy(value)
        if value.get("observed_at") == "NOW":
            value["observed_at"] = datetime.now(timezone.utc).isoformat()
        return value

    def backup_creator(**kwargs):
        events.append(("backup", kwargs))
        if isinstance(backup, Exception):
            raise backup
        from tests.test_dim_product_scd2_scenario_backup import _retained_proof

        default = _retained_proof() if retained_backup is True else _backup_result()
        return deepcopy(backup if backup is not None else default)

    def scenario_runner(**kwargs):
        events.append(("run", kwargs))
        value = run_evidence.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    result = execute_dim_product_scd2_scenario_run_and_rerun_proof(
        mapping or _mapping_probe(),
        scenario or _scenario_probe(),
        scenario_generation_review_status="approved",
        executor_review_status=review,
        approved=approved,
        approval_scope=scope,
        candidate_probe=candidate_probe,
        backup_creator=backup_creator,
        scenario_runner=scenario_runner,
        retained_backup=retained_backup,
    )
    return result, events


def test_sequence_requires_fresh_preflight_then_backup_then_two_runs_and_postflights():
    result, events = _execute(preflights=[
        {**_preflight(), "observed_at": "NOW"},
        _preflight(), _preflight(),
    ])
    assert result["status"] == "passed"
    assert result["scenario_run_attempt_count"] == 2
    assert result["successful_scenario_run_count"] == 2
    assert result["named_backup_retained"] is True
    assert result["automatic_rollback_performed"] is False
    assert result["source_count"] == result["target_count"] == 72
    assert [e if isinstance(e, str) else e[0] for e in events] == [
        "preflight", "backup", "run", "postflight", "run", "postflight"
    ]
    assert events[1][1] == {
        "owner": "DWH", "source_table": "DIM_PRODUCT",
        "backup_table": "ODI68_DIM_PRODUCT_BKP",
        "expected_column_count": 26, "expected_row_count": 72,
        "require_absent": True,
    }
    assert [e[1] for e in events if isinstance(e, tuple) and e[0] == "run"] == [
        {"approved": True, "approval_scope": SCOPE,
         "scenario_name": SCENARIO["name"], "version": "001", "context": "GLOBAL"},
    ] * 2


@pytest.mark.parametrize("approved,scope,review", [
    (False, SCOPE, "approved"), ("yes", SCOPE, "approved"),
    (True, "other", "approved"), (True, SCOPE, "pending"),
])
def test_bad_approval_or_review_blocks_before_callbacks(approved, scope, review):
    result, events = _execute(approved=approved, scope=scope, review=review)
    assert result["status"].startswith("blocked")
    assert events == []


def test_stale_preflight_blocks_before_backup():
    stale = _preflight()
    stale["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    result, events = _execute(preflights=[stale])
    assert result["status"] == "blocked_run_readiness"
    assert events == ["preflight"]


def test_invalid_mapping_or_scenario_blocks_before_backup():
    mapping = _mapping_probe()
    mapping["mapping"]["mapped_attribute_count"] = 25
    result, events = _execute(mapping=mapping)
    assert result["status"] == "blocked_run_readiness"
    assert events == ["preflight"]
    scenario = _scenario_probe()
    scenario["scenario"]["version"] = "002"
    result, events = _execute(scenario=scenario)
    assert result["status"] == "blocked_run_readiness"
    assert events == ["preflight"]


@pytest.mark.parametrize("bad", [
    {**_backup_result(), "status": "failed"},
    {**_backup_result(), "backup": {**_backup_result()["backup"], "column_count": 25}},
    RuntimeError("sensitive-value"),
])
def test_backup_failure_stops_before_run_and_reports_possible_partial_state(bad):
    result, events = _execute(backup=bad)
    assert result["status"] == "failed"
    assert result["stage"] == "named_backup_gate"
    assert result["scenario_run_attempt_count"] == 0
    assert result["backup_may_exist"] is True
    assert result["manual_recovery_review_required"] is True
    assert "sensitive-value" not in str(result)
    assert [e if isinstance(e, str) else e[0] for e in events] == ["preflight", "backup"]


@pytest.mark.parametrize("retained_backup", [False, True])
@pytest.mark.parametrize("stage,runs,preflights,attempts,successes", [
    ("first_scenario_run", [{**_run_result(), "status": "failed"}], None, 1, 0),
    ("first_count_only_postflight", None,
     [_preflight(), {**_preflight(), "candidate_count": 1}], 1, 1),
    ("second_scenario_run", [_run_result(), RuntimeError("sensitive-value")],
     None, 2, 1),
    ("second_count_only_postflight", None,
     [_preflight(), _preflight(), {**_preflight(), "target_count": 73}], 2, 2),
])
def test_partial_failure_states(stage, runs, preflights, attempts, successes, retained_backup):
    result, _events = _execute(runs=runs, preflights=preflights, retained_backup=retained_backup)
    assert result["status"] == "failed"
    assert result["stage"] == stage
    assert result["scenario_run_attempt_count"] == attempts
    assert result["successful_scenario_run_count"] == successes
    assert result["named_backup_retained"] is True
    assert result["automatic_rollback_performed"] is False
    assert result["manual_recovery_review_required"] is True
    assert result["backup_creation_attempted"] is (not retained_backup)
    assert result["backup_reused_verified"] is retained_backup
    assert result["backup_created_absent_only"] is (not retained_backup)
    assert "sensitive-value" not in str(result)


def test_read_only_preflight_exception_has_no_backup_or_scenario_attempt():
    result, events = _execute(preflights=[RuntimeError("sensitive-value")])
    assert result["status"] == "failed"
    assert result["stage"] == "fresh_read_only_preflight"
    assert result["scenario_run_attempt_count"] == 0
    assert result["backup_may_exist"] is False
    assert "sensitive-value" not in str(result)
    assert events == ["preflight"]


def test_rerun_rejects_count_drift_even_with_no_candidates():
    second = _preflight()
    second["source_count"] = second["target_count"] = 73
    second["current_target_count"] = second["unchanged_count"] = 73
    result, _ = _execute(preflights=[_preflight(), _preflight(), second])
    assert result["status"] == "failed"
    assert result["stage"] == "second_count_only_postflight"


def test_unsafe_run_result_is_never_propagated():
    bad = _run_result()
    bad["session_id_included"] = True
    bad["internal_id"] = "sensitive-value"
    result, events = _execute(runs=[bad])
    assert result["stage"] == "first_scenario_run"
    assert "sensitive-value" not in str(result)
    assert len([e for e in events if isinstance(e, tuple) and e[0] == "run"]) == 1


class _CustomDriverError(Exception):
    pass


def test_unexpected_exception_type_from_backup_creator_is_structured():
    result, events = _execute(backup=_CustomDriverError("sensitive-value"))
    assert result["status"] == "failed"
    assert result["stage"] == "named_backup_gate"
    assert result["backup_may_exist"] is True
    assert result["manual_recovery_review_required"] is True
    assert result["scenario_run_attempt_count"] == 0
    assert "sensitive-value" not in str(result)
    assert "_CustomDriverError" not in str(result)


def test_unexpected_exception_type_from_scenario_runner_is_structured():
    result, _ = _execute(runs=[_CustomDriverError("sensitive-value")])
    assert result["status"] == "failed"
    assert result["stage"] == "first_scenario_run"
    assert result["scenario_run_attempt_count"] == 1
    assert result["named_backup_retained"] is True
    assert result["manual_recovery_review_required"] is True
    assert "sensitive-value" not in str(result)
    assert "_CustomDriverError" not in str(result)


def _real_preflight_output():
    """Mirror the keys run_dim_product_scd2_candidate_preflight really emits."""
    return {
        "mode": "dim_product_scd2_candidate_preflight",
        "status": "rerun_noop",
        "requires_user_approval_for_persistent_action": True,
        "connection_attempted": True,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "source_count": 72,
        "target_count": 72,
        "source_duplicate_groups": 0,
        "duplicate_product_sk_groups": 0,
        "duplicate_current_natural_key_groups": 0,
        "invalid_current_flags": 0,
        "invalid_effective_dates": 0,
        "current_target_count": 72,
        "insert_without_current_count": 0,
        "changed_current_count": 0,
        "unchanged_current_count": 72,
        "source_missing_current_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "candidate_count": 0,
    }


def test_real_preflight_output_adapts_to_gates_and_reaches_ready():
    from dim_product_odi_scd2_scenario import (
        build_dim_product_scd2_scenario_run_readiness,
        candidate_preflight_to_gate_report,
    )
    from dim_product_scd2_scenario_backup import _preflight_matches

    started = datetime.now(timezone.utc)
    raw = _real_preflight_output()
    raw["unexpected_extra"] = "sensitive-value"
    adapted = candidate_preflight_to_gate_report(raw, datetime.now(timezone.utc))
    assert "unexpected_extra" not in adapted
    assert "source_duplicate_groups" not in adapted
    assert adapted["observed_at"].endswith("+00:00")
    assert _preflight_matches(adapted)
    readiness = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(), _scenario_probe(), adapted,
        scenario_generation_review_status="approved", approved=True,
        approval_scope=SCOPE, executor_started_at=started,
    )
    assert readiness["status"] == "ready_for_separate_scenario_run_executor"


def test_adapter_rejects_naive_timestamp_and_nonmapping():
    from dim_product_odi_scd2_scenario import candidate_preflight_to_gate_report

    assert candidate_preflight_to_gate_report(_real_preflight_output(), datetime.now()) == {}
    assert candidate_preflight_to_gate_report(None, datetime.now(timezone.utc)) == {}


def test_adapter_does_not_invent_missing_keys():
    from dim_product_odi_scd2_scenario import candidate_preflight_to_gate_report
    from dim_product_scd2_scenario_backup import _preflight_matches

    raw = _real_preflight_output()
    del raw["unchanged_current_count"]
    assert not _preflight_matches(
        candidate_preflight_to_gate_report(raw, datetime.now(timezone.utc))
    )


def test_real_wrapper_output_satisfies_executor_run_matches(monkeypatch):
    import json
    import odi_orchestrator
    from dim_product_odi_scd2_scenario_run_executor import (
        _run_matches,
        scenario_runner,
    )

    def fake_run(self, script, params):
        return json.dumps({
            "tool": "odi.dim_product_scd2_scenario_run",
            "mode": "fixed_scd2_scenario_run",
            "repository_connection_attempted": True,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "scenario": dict(SCENARIO),
            "status": "passed",
            "side_effects": True,
            "session_status": "DONE",
        })

    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "run_groovy", fake_run)
    result = scenario_runner(
        approved=True, approval_scope=SCOPE,
        scenario_name=SCENARIO["name"], version=SCENARIO["version"],
        context=SCENARIO["context"],
    )
    assert result["status"] == "passed"
    assert _run_matches(result)


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}, 0.0])
def test_retained_mode_requires_exact_bool_before_any_callbacks(value):
    result, events = _execute(retained_backup=value)
    assert result["status"] == "blocked_retained_backup_type"
    assert events == []
    assert result["backup_creation_attempted"] is False


def test_opt_in_retained_mode_verifies_before_two_runs_without_claiming_creation():
    result, events = _execute(retained_backup=True)
    assert result["status"] == "passed"
    assert [e if isinstance(e, str) else e[0] for e in events] == [
        "preflight", "backup", "run", "postflight", "run", "postflight"
    ]
    assert events[1][1]["require_absent"] is False
    assert result["backup_mode"] == "retained_verification"
    assert result["backup_creation_attempted"] is False
    assert result["backup_verification_attempted"] is True
    assert result["backup_reused_verified"] is True
    assert result["backup_created_absent_only"] is False
    assert result["named_backup_retained"] is True
    assert result["persistent_action_attempted"] is True  # the scenario callback did run


@pytest.mark.parametrize("bad", [
    _backup_result(), {"status": "failed", "side_effects": False},
    RuntimeError("sensitive-value"),
])
def test_retained_verification_failure_stops_runs_without_claiming_any_persistent_attempt(bad):
    result, events = _execute(backup=bad, retained_backup=True)
    assert result["status"] == "failed"
    assert result["stage"] == "named_backup_gate"
    assert result["backup_creation_attempted"] is False
    assert result["backup_verification_attempted"] is True
    assert result["backup_reused_verified"] is False
    assert result["backup_created_absent_only"] is False
    assert result["persistent_action_attempted"] is False
    assert result["backup_may_exist"] is True
    assert result["named_backup_retained"] is None
    assert result["manual_recovery_review_required"] is True
    assert result["automatic_rollback_performed"] is False
    assert result["scenario_run_attempt_count"] == 0
    assert [e if isinstance(e, str) else e[0] for e in events] == ["preflight", "backup"]
    assert "sensitive-value" not in repr(result)


def test_retained_proof_cannot_enable_default_mode():
    from tests.test_dim_product_scd2_scenario_backup import _retained_proof

    result, events = _execute(backup=_retained_proof())
    assert result["status"] == "failed"
    assert result["stage"] == "named_backup_gate"
    assert not any(isinstance(e, tuple) and e[0] == "run" for e in events)


@pytest.mark.parametrize("retained_backup", [False, True])
@pytest.mark.parametrize("run_index", [0, 1])
@pytest.mark.parametrize("error_type", [
    "AgentConfigurationUnavailable", "AgentUnavailable", "ScenarioRunFailed", "InvalidScenarioRunOutput",
])
def test_only_allowlisted_failed_run_diagnostic_is_preserved(error_type, run_index, retained_backup):
    failure = {
        "status": "failed", "error_type": error_type,
        "error": "sensitive-value", "body": {"row": "sensitive-value"},
    }
    result, _ = _execute(runs=[_run_result()] * run_index + [failure], retained_backup=retained_backup)
    assert result["status"] == "failed"
    assert result["error_type"] == error_type
    assert result["stage"] == ("first_scenario_run" if run_index == 0 else "second_scenario_run")
    assert result["scenario_run_attempt_count"] == run_index + 1
    assert result["backup_creation_attempted"] is (not retained_backup)
    assert "sensitive-value" not in repr(result)


@pytest.mark.parametrize("error_type", [
    "sensitive-value", "AgentUnavailable\nsensitive-value", "agentUnavailable", "", None,
    True, 1, [], {}, ["AgentUnavailable"], {"AgentUnavailable": True},
])
@pytest.mark.parametrize("run_index", [0, 1])
def test_hostile_unknown_or_nonstring_run_diagnostics_are_not_preserved(error_type, run_index):
    result, _ = _execute(runs=[_run_result()] * run_index + [{
        "status": "failed", "error_type": error_type, "body": "sensitive-value",
    }])
    assert result["error_type"] == "ScenarioRunError"
    assert "sensitive-value" not in repr(result)


def test_allowlisted_code_does_not_turn_malformed_success_into_trusted_failure():
    result, _ = _execute(runs=[{**_run_result(), "error_type": "AgentUnavailable"}])
    assert result["status"] == "failed"
    assert result["error_type"] == "ScenarioRunError"
