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
        "checks": [{"name": "scenario_found", "status": "pass", "detail": "found"}],
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
    for malformed in (None, {"status": "error"}):
        report = proof.scenario_probe_to_gate_report(malformed)
        assert report["status"] == "error" and report["error_type"] == "InconsistentScenarioProbe"
        assert report["scenario"]["exists"] is False
    raw = _raw_scenario_probe()
    raw["scenario"]["name"] = "OTHER"
    assert proof.scenario_probe_to_gate_report(raw)["scenario"]["exists"] is False
    raw = _raw_scenario_probe()
    raw["side_effects"] = True
    assert proof.scenario_probe_to_gate_report(raw)["scenario"]["exists"] is False
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


# ---- review round 2 ----

def test_report_is_allowlisted_in_stdout_and_file(evidence, tmp_path):
    spy = Spy()
    leaky = {
        "status": "failed", "stage": "first_scenario_run", "error_type": "ScenarioRunError",
        "target_count": 72,
        "session_id": "SID-LEAK-1", "repository_id": "REPO-LEAK-2", "pk_value": "PK-LEAK-3",
        "message": "LEAK", "token": "LEAK",
        "scenario": {**SCENARIO, "session_id": "SID-LEAK-5", "repository_id": "REPO-LEAK-6"},
    }
    wiring = _wire(spy, executor=spy("executor", leaky))
    assert proof.run(_flat(_args(evidence, tmp_path)), **wiring) == proof.EXIT_NOT_PASSED
    stdout = wiring["stdout"].getvalue()
    written = (tmp_path / "out.json").read_text()
    for text in (stdout, written):
        assert "LEAK" not in text
        report = json.loads(text)
        assert report["status"] == "failed" and report["target_count"] == 72
        assert report["scenario"] == SCENARIO
        assert set(report) <= proof._REPORT_FIELDS | {"scenario"}


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(status="passed", repository_connection_attempted=False),
    lambda r: r.update(checks=[]),
    lambda r: r.update(checks=[{"name": "scenario_found", "status": "fail"}]),
    lambda r: r.update(tool="odi.other"),
    lambda r: r.update(mode="other_mode"),
    lambda r: r.pop("repository_connection_attempted"),
    lambda r: r["scenario"].pop("latest_version"),
    lambda r: r["scenario"].update(latest_version="002"),
])
def test_scenario_probe_adapter_requires_consistent_passed_probe(mutate):
    raw = _raw_scenario_probe()
    assert proof.scenario_probe_to_gate_report(raw)["scenario"]["exists"] is True
    mutate(raw)
    report = proof.scenario_probe_to_gate_report(raw)
    assert report.get("scenario", {}).get("exists", False) is False
    assert report["status"] == "error"
    assert report.get("error_type", "InconsistentScenarioProbe") == "InconsistentScenarioProbe"


def test_scenario_probe_adapter_old_fixture_without_found_check_is_not_exists():
    raw = _raw_scenario_probe()
    raw["checks"] = []
    report = proof.scenario_probe_to_gate_report(raw)
    assert report["scenario"]["exists"] is False
    assert report["error_type"] == "InconsistentScenarioProbe"


@pytest.mark.parametrize("value", [[], {}, ["passed"], {"status": "passed"}, None, True, 1])
@pytest.mark.parametrize("field", ["status", "check_status"])
def test_non_string_probe_status_returns_consistent_error(value, field):
    raw = _raw_scenario_probe()
    if field == "status":
        raw["status"] = value
    else:
        raw["checks"][0]["status"] = value
    report = proof.scenario_probe_to_gate_report(raw)
    assert report["status"] == "error"
    assert report["error_type"] == "InconsistentScenarioProbe"
    assert report["scenario"]["exists"] is False
    assert report["side_effects"] is False


@pytest.mark.parametrize("kind", ["missing_parent", "is_directory", "empty"])
def test_invalid_out_path_exits_2_before_any_callback(evidence, tmp_path, kind):
    spy = Spy()
    values = _args(evidence, tmp_path)
    values["--out"] = {
        "missing_parent": str(tmp_path / "nope" / "out.json"),
        "is_directory": str(tmp_path),
        "empty": "",
    }[kind]
    wiring = _wire(spy)
    assert proof.run(_flat(values), **wiring) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []
    assert json.loads(wiring["stdout"].getvalue())["status"] == "blocked_output_path"
    assert not (tmp_path / "out.json").exists()


def test_existing_unwritable_out_path_is_rejected_before_callbacks(evidence, tmp_path, monkeypatch):
    target = tmp_path / "out.json"
    target.write_text("original", encoding="utf-8")
    real_open = proof.os.open

    def deny_existing(path, flags, *args, **kwargs):
        if Path(path) == target.resolve():
            raise PermissionError("read-only target")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(proof.os, "open", deny_existing)
    spy = Spy()
    wiring = _wire(spy)
    assert proof.run(_flat(_args(evidence, tmp_path)), **wiring) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []
    assert target.read_text(encoding="utf-8") == "original"
    assert json.loads(wiring["stdout"].getvalue())["status"] == "blocked_output_path"


def test_report_write_failure_exits_3_and_stdout_keeps_report(evidence, tmp_path, monkeypatch):
    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    spy = Spy()
    wiring = _wire(spy, executor=spy("executor", {"status": "passed", "target_count": 72}))
    monkeypatch.setattr(Path, "write_text", boom)
    assert proof.run(_flat(_args(evidence, tmp_path)), **wiring) == proof.EXIT_REPORT_WRITE_FAILED
    stdout = wiring["stdout"].getvalue()
    assert '"status": "passed"' in stdout and '"target_count": 72' in stdout
    assert "report_write_failed" in stdout and "disk full" not in stdout


@pytest.mark.parametrize("payload", [
    {"executor_review_status": "approved", "scenario_generation_review_status": "approved",
     "note": "extra"},
    {"executor_review_status": "approved", "scenario_generation_review_status": ["approved"]},
    {"executor_review_status": True, "scenario_generation_review_status": "approved"},
    {"executor_review_status": "Approved", "scenario_generation_review_status": "approved"},
])
def test_review_evidence_requires_exactly_two_approved_strings(tmp_path, payload):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload))
    spy = Spy()
    wiring = _wire(spy)
    assert proof.run(_flat(_args(path, tmp_path)), **wiring) == proof.EXIT_BAD_ARGUMENTS
    assert spy.calls == []
    assert json.loads(wiring["stdout"].getvalue())["status"] == "blocked_review_evidence"
    assert not (tmp_path / "out.json").exists()


def test_help_documents_exit_codes():
    out = io.StringIO()
    assert proof.run(["--help"], stdout=out) == 0
    assert "3 the run finished but the report file could not be written" in " ".join(out.getvalue().split())
