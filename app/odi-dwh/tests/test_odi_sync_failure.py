import subprocess
import sys

import pytest

import odi_orchestrator
from schema_helpers import _schema_change_sync_odi_payload

_SYNC_BODY = {
    "approved": True,
    "model_code": "TGT_DWH",
    "table": "ODS_HR_EMPLOYEES",
    "column": "FIRST_NAME",
    "op": "modify_type",
    "new_type": "VARCHAR2(30)",
}


@pytest.fixture
def groovy_runner(monkeypatch):
    monkeypatch.setattr(odi_orchestrator, "_JAVA_BIN", sys.executable)
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: "synthetic-runtime-config")

    def set_result(*, returncode=0, stdout="SUCCESS: ODI datastore metadata sync committed", stderr=""):
        monkeypatch.setattr(
            odi_orchestrator.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout, stderr),
        )

    return set_result


def test_nonzero_groovy_exit_reports_failed_sync(groovy_runner):
    groovy_runner(returncode=7)

    result = _schema_change_sync_odi_payload(_SYNC_BODY)

    assert result["applied"] is False
    assert result["status"] == "ODI_METADATA_SYNC_FAILED"
    assert "GroovyProcessError" in result["reason"]
    assert "result" not in result


def test_groovy_runner_exception_reports_failed_sync(groovy_runner, monkeypatch):
    groovy_runner()

    def raise_error(*_args, **_kwargs):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(odi_orchestrator.subprocess, "run", raise_error)

    result = _schema_change_sync_odi_payload(_SYNC_BODY)

    assert result["applied"] is False
    assert result["status"] == "ODI_METADATA_SYNC_FAILED"
    assert "RuntimeError" in result["reason"]
    assert "private diagnostic" not in str(result)


def test_groovy_timeout_reports_failed_sync(groovy_runner, monkeypatch):
    groovy_runner()

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("groovy", 300)

    monkeypatch.setattr(odi_orchestrator.subprocess, "run", raise_timeout)

    result = _schema_change_sync_odi_payload(_SYNC_BODY)

    assert result["applied"] is False
    assert result["status"] == "ODI_METADATA_SYNC_FAILED"
    assert "TimeoutExpired" in result["reason"]


def test_groovy_error_marker_reports_failed_sync(groovy_runner):
    groovy_runner(stdout='{"status":"error","action":"sync_datastore_column"}')

    result = _schema_change_sync_odi_payload(_SYNC_BODY)

    assert result["applied"] is False
    assert result["status"] == "ODI_METADATA_SYNC_FAILED"
    assert "reported an error" in result["reason"] or "GroovyFatalOutput" in result["reason"]


def test_successful_groovy_sync_preserves_success_response(groovy_runner):
    groovy_runner()

    result = _schema_change_sync_odi_payload(_SYNC_BODY)

    assert result["applied"] is True
    assert result["status"] == "ODI_METADATA_SYNCED"
    assert result["result"] == "SUCCESS: ODI datastore metadata sync committed"


@pytest.mark.parametrize("output", [
    "", "ODI runtime initialized", '{"status":"success","action":"unrelated"}',
    "SUCCESS: ODI datastore metadata synced",
    "prefix SUCCESS: ODI datastore metadata sync committed",
    "SUCCESS: ODI datastore metadata sync committed trailing text",
    "SUCCESS: ODI datastore metadata sync committed\n" * 2,
])
def test_zero_exit_without_unique_commit_acknowledgement_is_not_success(groovy_runner, output):
    groovy_runner(stdout=output)
    result = _schema_change_sync_odi_payload(_SYNC_BODY)
    assert result["applied"] is False
    assert result["status"] == "ODI_METADATA_SYNC_FAILED"
    assert result["reason"] == "ODI Groovy did not confirm the metadata sync commit."
    assert "result" not in result


def test_commit_acknowledgement_accepts_normal_startup_lines(groovy_runner):
    groovy_runner(stdout="Runtime ready\n  SUCCESS: ODI datastore metadata sync committed  \n")
    assert _schema_change_sync_odi_payload(_SYNC_BODY)["applied"] is True


def test_error_marker_wins_over_commit_acknowledgement(groovy_runner):
    groovy_runner(stdout='SUCCESS: ODI datastore metadata sync committed\n'
                         '{"status":"error","action":"sync_datastore_column"}')
    result = _schema_change_sync_odi_payload(_SYNC_BODY)
    assert result["applied"] is False
    assert "result" not in result
