"""Argument gates and fully faked end-to-end wiring for the live-proof operator script."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import odi68_dim_product_scd2_live_proof as proof
from dim_product_scd2_backup_creator import create_dim_product_named_backup
from tests.test_dim_product_scd2_backup_creator import FakeDb

SCOPE = "odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof"
SCENARIO = {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN", "version": "001", "context": "GLOBAL"}


def _mapping_probe():
    return {
        "tool": "odi.dim_product_scd2_mapping_probe", "status": "passed",
        "mode": "normalized_fixed_scd2_mapping_metadata", "side_effects": False,
        "repository_connection_attempted": True, "raw_rows_included": False,
        "pk_values_included": False, "secret_printed": False,
        "mapping": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT", "source_component_count": 1,
            "target_component_count": 1, "target_attribute_count": 26,
            "mapped_attribute_count": 26, "integration_type": "SCD",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "access_point_count": 0, "lkm_assigned_count": 0,
        },
    }


def _raw_scenario_probe():
    return {
        "tool": "odi.scenario_probe", "status": "passed", "mode": "scenario_finders_only",
        "side_effects": False, "repository_connection_attempted": True,
        "scenario": {"name": SCENARIO["name"], "version": "001", "latest_version": "001"},
        "checks": [],
    }


def _scenario_probe():
    return proof.scenario_probe_to_gate_report(_raw_scenario_probe())


def _raw_preflight():
    """Real candidate-preflight key names (the adapter must map them)."""
    return {
        "mode": "dim_product_scd2_candidate_preflight", "status": "rerun_noop",
        "requires_user_approval_for_persistent_action": True, "connection_attempted": True,
        "will_execute": False, "write_attempted": False, "raw_rows_included": False,
        "pk_values_included": False, "secret_printed": False,
        "source_count": 72, "target_count": 72, "current_target_count": 72,
        "source_duplicate_groups": 0, "duplicate_product_sk_groups": 0,
        "duplicate_current_natural_key_groups": 0, "invalid_current_flags": 0,
        "invalid_effective_dates": 0, "insert_without_current_count": 0,
        "changed_current_count": 0, "unchanged_current_count": 72,
        "source_missing_current_count": 0, "candidate_count": 0,
        "backup_exists": 1, "backup_column_count": 24, "backup_row_count": 0,
    }


def _run_result():
    return {
        "status": "passed", "scenario": dict(SCENARIO), "session_status": "DONE",
        "side_effects": True, "raw_rows_included": False, "pk_values_included": False,
        "raw_output_included": False, "session_id_included": False, "secret_printed": False,
    }


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps({
        "executor_review_status": "approved",
        "scenario_generation_review_status": "approved",
    }))
    return path


class Spy:
    """Records which fake callbacks were invoked."""

    def __init__(self):
        self.calls = []

    def __call__(self, name, result=None, error=None):
        def callback(*args, **kwargs):
            self.calls.append(name)
            if error:
                raise error
            return result() if callable(result) else result
        return callback


def _args(evidence, tmp_path):
    return {"--approved": "yes", "--scope": SCOPE,
            "--review-evidence": str(evidence), "--out": str(tmp_path / "out.json")}


def _flat(values):
    return [item for pair in values.items() for item in pair]


def _wire(spy, **extra):
    return dict(
        executor=extra.pop("executor", spy("executor", {"status": "passed"})),
        mapping_probe_loader=spy("mapping", _mapping_probe),
        scenario_probe_loader=spy("scenario", _scenario_probe),
        candidate_runner=spy("candidate", _raw_preflight),
        backup_creator=spy("backup", None),
        scenario_runner=spy("runner", None),
        stdout=io.StringIO(), **extra,
    )


@pytest.mark.parametrize("overrides,status", [
    ({"--approved": "no"}, "blocked_until_exact_run_approval"),
    ({"--approved": "YES"}, "blocked_until_exact_run_approval"),
    ({"--approved": "true"}, "blocked_until_exact_run_approval"),
    ({"--scope": "wrong_scope"}, "blocked_run_approval_scope"),
    ({"--scope": ""}, "blocked_run_approval_scope"),
])
def test_invalid_approval_or_scope_exits_2_and_invokes_no_callback(evidence, tmp_path, overrides, status):
    spy = Spy()
    wiring = _wire(spy)
    values = {**_args(evidence, tmp_path), **overrides}
    assert proof.run(_flat(values), **wiring) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []
    assert json.loads(wiring["stdout"].getvalue())["status"] == status
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("missing", ["--approved", "--scope", "--review-evidence", "--out"])
def test_missing_argument_exits_2_and_invokes_no_callback(evidence, tmp_path, missing):
    spy = Spy()
    values = _args(evidence, tmp_path)
    del values[missing]
    assert proof.run(_flat(values), **_wire(spy)) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []


def test_unknown_argument_is_refused(evidence, tmp_path):
    spy = Spy()
    argv = _flat(_args(evidence, tmp_path)) + ["--force", "1"]
    assert proof.run(argv, **_wire(spy)) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []


@pytest.mark.parametrize("content", [
    None, "not json", "[]", "{}",
    json.dumps({"executor_review_status": "approved"}),
    json.dumps({"executor_review_status": "approved", "scenario_generation_review_status": "pending"}),
])
def test_bad_review_evidence_exits_2_and_invokes_no_callback(tmp_path, content):
    path = tmp_path / "review.json"
    if content is not None:
        path.write_text(content)
    spy = Spy()
    wiring = _wire(spy)
    assert proof.run(_flat(_args(path, tmp_path)), **wiring) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []
    assert json.loads(wiring["stdout"].getvalue())["status"] == "blocked_review_evidence"


def test_candidate_probe_adapts_real_preflight_with_fresh_utc_stamp(evidence, tmp_path):
    captured = {}

    def executor(mapping, scenario, *, candidate_probe, **kwargs):
        captured["candidate"] = candidate_probe()
        captured["kwargs"] = kwargs
        return {"status": "passed"}

    spy = Spy()
    stamp = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    wiring = _wire(spy, executor=executor, clock=lambda: stamp)
    assert proof.run(_flat(_args(evidence, tmp_path)), **wiring) == 0
    cand = captured["candidate"]
    assert cand["observed_at"] == stamp.isoformat()
    assert cand["source_duplicate_key_group_count"] == 0 and cand["unchanged_count"] == 72
    assert captured["kwargs"]["approved"] is True
    assert captured["kwargs"]["approval_scope"] == SCOPE


@pytest.mark.parametrize("status,code", [("passed", 0), ("failed", 1), ("blocked_named_backup_gate", 1)])
def test_exit_code_follows_executor_status_and_report_is_sanitized(evidence, tmp_path, status, code):
    spy = Spy()
    leaky = {"status": status, "session_id": "SID-1", "rows": [1, 2], "target_count": 72}
    wiring = _wire(spy, executor=spy("executor", leaky))
    assert proof.run(_flat(_args(evidence, tmp_path)), **wiring) == code
    written = json.loads((tmp_path / "out.json").read_text())
    assert written["status"] == status and written["target_count"] == 72
    assert "rows" not in written  # non-scalar fields are dropped


def test_scenario_probe_adapter_rejects_unexpected_shapes():
    assert proof.scenario_probe_to_gate_report(None) == {"status": "error"}
    assert proof.scenario_probe_to_gate_report({"status": "error"}) == {"status": "error"}
    raw = _raw_scenario_probe()
    raw["scenario"]["name"] = "OTHER"
    assert proof.scenario_probe_to_gate_report(raw) == {"status": "error"}
    raw = _raw_scenario_probe()
    raw["side_effects"] = True
    assert proof.scenario_probe_to_gate_report(raw) == {"status": "error"}
    absent = _raw_scenario_probe()
    absent["status"] = "attention_required"
    assert proof.scenario_probe_to_gate_report(absent)["scenario"]["exists"] is False
    assert _scenario_probe()["scenario"]["exists"] is True


def test_probe_loader_exception_blocks_run_without_leaking(evidence, tmp_path):
    spy = Spy()
    seen = {}

    def executor(mapping, scenario, **kwargs):
        seen["mapping"] = mapping
        return {"status": "blocked_run_readiness"}

    wiring = _wire(spy, executor=executor)
    wiring["mapping_probe_loader"] = spy("mapping", error=RuntimeError("secret"))
    assert proof.run(_flat(_args(evidence, tmp_path)), **wiring) == proof.EXIT_NOT_PASSED
    assert seen["mapping"] == {}
    assert "secret" not in wiring["stdout"].getvalue()


def test_end_to_end_all_callbacks_faked_ready_run_postflight_rerun_noop(evidence, tmp_path):
    db = FakeDb()
    events = []

    def candidate():
        events.append("preflight")
        return _raw_preflight()

    def backup(**kwargs):
        events.append("backup")
        return create_dim_product_named_backup(db, **kwargs)

    def runner(**kwargs):
        events.append("run")
        assert kwargs["approved"] is True and kwargs["approval_scope"] == SCOPE
        return _run_result()

    out = io.StringIO()
    code = proof.run(
        _flat(_args(evidence, tmp_path)),
        mapping_probe_loader=_mapping_probe, scenario_probe_loader=_scenario_probe,
        candidate_runner=candidate, backup_creator=backup, scenario_runner=runner, stdout=out,
    )
    report = json.loads(out.getvalue())
    assert code == 0, report
    assert report["status"] == "passed"
    assert events == ["preflight", "backup", "run", "preflight", "run", "preflight"]
    assert report["scenario_run_attempt_count"] == 2
    assert report["successful_scenario_run_count"] == 2
    assert report["remaining_candidate_count"] == 0
    assert report["named_backup_retained"] is True
    assert report["automatic_rollback_performed"] is False
    assert (report["named_backup_column_count"], report["named_backup_row_count"]) == (26, 72)
    assert json.loads((tmp_path / "out.json").read_text()) == report
    assert any(s.upper().startswith("CREATE TABLE") for s in db.statements)
    assert not any(w in s.upper() for s in db.statements for w in ("DROP", "DELETE", "TRUNCATE"))


def test_end_to_end_existing_backup_blocks_before_any_run(evidence, tmp_path):
    db = FakeDb(backup_exists=True)
    runner_calls = []
    out = io.StringIO()
    code = proof.run(
        _flat(_args(evidence, tmp_path)),
        mapping_probe_loader=_mapping_probe, scenario_probe_loader=_scenario_probe,
        candidate_runner=_raw_preflight,
        backup_creator=lambda **kw: create_dim_product_named_backup(db, **kw),
        scenario_runner=lambda **kw: runner_calls.append(kw) or _run_result(), stdout=out,
    )
    report = json.loads(out.getvalue())
    assert code == proof.EXIT_NOT_PASSED and report["status"] == "failed"
    assert report["stage"] == "named_backup_gate" and runner_calls == []
    assert report["automatic_rollback_performed"] is False


def test_script_is_not_a_route_and_defers_config_and_driver_imports():
    text = Path(proof.__file__).read_text(encoding="utf-8")
    assert "Blueprint" not in text and "@app." not in text
    head = text.split("def _configured_ddl_connection")[0]
    assert "\nimport config" not in head and "\nimport oracledb" not in head
