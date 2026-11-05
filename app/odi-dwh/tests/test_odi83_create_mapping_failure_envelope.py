"""ODI-83: the create_mapping structured failure survives its deliberate exit 1, allowlisted only."""
import json
import subprocess

import pytest

import mapping_generator
import odi_orchestrator
from odi_orchestrator import OdiOrchestrator, groovy_approval_context

GENERIC = "Hata: ODI Groovy action failed. error_type=GroovyProcessError"


def _line(**overrides):
    payload = {
        "status": "error",
        "action": "create_mapping",
        "replaced": False,
        "kept_existing_on_failure": True,
        "error_type": "IllegalStateException",
        "validation_messages": ["ERROR: password=hunter2 jdbc:oracle:thin:@db:1521/x"],
        "physical_design": {"access_points": 1, "access_point_names": ["SRC_AP"]},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _run(monkeypatch, *, stdout, returncode=1, stderr="", script="create_mapping.groovy"):
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(OdiOrchestrator, "_build_classpath", lambda _self: "cp")
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: "local.properties")
    monkeypatch.setattr(
        odi_orchestrator.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], returncode, stdout, stderr),
    )
    with groovy_approval_context(True):
        return OdiOrchestrator().run_groovy(script, {})


@pytest.mark.parametrize("kept", [True, False])
def test_structured_failure_survives_nonzero_exit(monkeypatch, kept):
    output = _run(
        monkeypatch,
        stdout="PARAMS: mapping=M\nprogress\n" + _line(kept_existing_on_failure=kept) + "\n",
    )
    state = mapping_generator.mapping_replace_state_from_output(output)
    assert state == {
        "replaced": False,
        "kept_existing_on_failure": kept,
        "error_type": "IllegalStateException",
    }
    assert mapping_generator.create_mapping_output_failed(output) is True
    assert output.splitlines()[0].startswith("Hata:")


def test_only_allowlisted_fields_and_no_free_text_leave_run_groovy(monkeypatch):
    output = _run(
        monkeypatch,
        stdout="connection user=scott password=tiger\n" + _line() + "\n",
        stderr="SEVERE: password=hunter3 failure detail",
    )
    assert json.loads(output.splitlines()[1]) == {
        "status": "error",
        "action": "create_mapping",
        "replaced": False,
        "kept_existing_on_failure": True,
        "error_type": "IllegalStateException",
    }
    for marker in ("hunter", "tiger", "scott", "jdbc", "SRC_AP", "PARAMS", "SEVERE"):
        assert marker not in output


def test_envelope_with_redaction_breaking_free_text_still_survives(monkeypatch):
    output = _run(monkeypatch, stdout=_line(validation_messages=['user=abc"def', "token=x"]) + "\n")
    assert mapping_generator.mapping_replace_state_from_output(output)["kept_existing_on_failure"] is True
    assert "abc" not in output


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "plain progress only\n",
        "not json {\n",
        "[1, 2]\n",
        _line() + "\ntrailing line after the envelope\n",
        _line(status="passed") + "\n",
        _line(status="failed") + "\n",
        _line(action="update_mapping") + "\n",
        _line(replaced=True) + "\n",
        _line(replaced="false") + "\n",
        _line(kept_existing_on_failure="true") + "\n",
        _line(kept_existing_on_failure=1) + "\n",
        _line(kept_existing_on_failure=None) + "\n",
        _line(error_type="ORA-00942: table or view") + "\n",
        _line(error_type="") + "\n",
        _line(error_type=None) + "\n",
        _line(error_type="A" * 129) + "\n",
        _line(extra="surprise") + "\n",
        _line(validation_messages="not a list") + "\n",
        _line(physical_design=[]) + "\n",
        json.dumps({"status": "error", "action": "create_mapping", "replaced": False,
                    "kept_existing_on_failure": True, "error_type": "E"}) + "\n",
    ],
)
def test_malformed_output_falls_back_to_generic_failure(monkeypatch, stdout):
    assert _run(monkeypatch, stdout=stdout) == GENERIC


@pytest.mark.parametrize("returncode", [2, 137, -1])
def test_unexpected_exit_code_is_not_trusted(monkeypatch, returncode):
    assert _run(monkeypatch, stdout=_line() + "\n", returncode=returncode) == GENERIC


def test_other_scripts_do_not_gain_the_envelope(monkeypatch):
    output = _run(monkeypatch, stdout=_line() + "\n", script="update_mapping.groovy")
    assert output == GENERIC


def test_generic_failure_reports_no_retained_existing(monkeypatch):
    state = mapping_generator.mapping_replace_state_from_output(_run(monkeypatch, stdout="noise\n"))
    assert state["kept_existing_on_failure"] is False
    assert state["error_type"] == "GroovyProcessError"
