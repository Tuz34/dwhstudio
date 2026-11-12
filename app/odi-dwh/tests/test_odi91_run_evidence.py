"""ODI-91: session number, count-only evidence and a safe ERROR code.

Offline only. These tests must fail on the pre-change contract and service.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import mapping_run_service as service
import mapping_state_manager as state_module
import odi_orchestrator as oo
import remote_run_contract as contract
from tests.test_remote_run_contract import done, generated, healthy, setup_pipeline


ROOT = Path(__file__).resolve().parents[1]
GROOVY = ROOT / "odi_automation" / "run_scenario_remote.groovy"
ODI_MCP = ROOT / "odi_mcp.py"

HOSTILE_ERROR = (
    'INSERT INTO "HR"."EMPLOYEES" VALUES (\'O\'\'Brien\', 1) failed: '
    "ORA-00932: inconsistent datatypes: expected NUMBER got CHAR"
)
COUNTS = {"processed": 10, "inserted": 8, "updated": 2, "errors": 0}
READER_ROW = {
    "SESSION_NO": 311,
    "PROCESSED_ROWS": 10,
    "INSERTED_ROWS": 8,
    "UPDATED_ROWS": 2,
    "ERROR_ROWS": 0,
    "ERROR_MESSAGE": HOSTILE_ERROR,
}
PRE_CHANGE_STATE = {
    "batch_id": "RUN-OLDSTATE01",
    "mapping_name": "MAP_TEST",
    "scenario_name": "MAP_TEST_SCN",
    "scenario_version": "001",
    "context": "GLOBAL",
    "truncate_target_table": False,
    "current_step": "run_scenario",
    "overall_status": "SUCCEEDED",
    "completed_steps": ["update_km", "generate_scenario", "run_scenario"],
    "error": None,
    "validated_params": {
        "mapping_name": "MAP_TEST",
        "scenario_name": "MAP_TEST_SCN",
        "scenario_version": "001",
        "context": "GLOBAL",
        "truncate_target_table": False,
    },
}


def failed_run():
    return contract.result(
        "run", status="failed", stage="run", error_code="ScenarioRunFailed",
        agent=True, repository=True, may_have_started=True, session_status="ERROR",
    )


def with_session(payload: dict, number: object = 311) -> dict:
    body = dict(payload)
    body["session_number"] = number
    return body


def _options():
    return service.MappingRunOptions("MAP_TEST", "001", "GLOBAL", False)


def _run(monkeypatch, remote, reader=None):
    setup_pipeline(monkeypatch, run=remote)
    if reader is not None:
        monkeypatch.setattr(service, "_read_session_row", reader)
    return service.execute_mapping_run(Mock(), _options())


def _today_success_keys(payload: dict) -> set[str]:
    return set(payload) - {"session_number", "counts", "session_error_code", "batch_id"}


# --- remote_run_contract: session_number ------------------------------------

@pytest.mark.parametrize("number", [1, 311, 2_147_483_647])
def test_parse_accepts_a_positive_session_number(number):
    parsed = contract.parse(with_session(done(), number), "run")
    assert parsed["status"] == "passed"
    assert parsed["session_status"] == "DONE"
    assert parsed["outcome_unknown"] is False
    assert parsed["session_number"] == number
    assert parsed["error_code"] is None


def test_parse_accepts_null_session_number_and_keeps_today_outcome():
    parsed = contract.parse(with_session(done(), None), "run")
    assert parsed["status"] == "passed"
    assert parsed["session_number"] is None
    assert parsed["session_status"] == "DONE"


def test_parse_accepts_an_old_payload_without_session_number():
    payload = done()
    assert "session_number" not in payload
    parsed = contract.parse(payload, "run")
    assert parsed["status"] == "passed"
    assert parsed["session_status"] == "DONE"
    assert "session_number" not in parsed
    assert parsed["error_code"] is None
    assert parsed["outcome_unknown"] is False


@pytest.mark.parametrize("number", [0, -1, 1.5, "311", True, False, [], {}, "null"])
def test_parse_rejects_non_positive_integer_session_numbers(number):
    parsed = contract.parse(with_session(done(), number), "run")
    assert parsed["status"] == "unknown"
    assert parsed["outcome_unknown"] is True
    assert parsed["error_code"] == "RemoteOutputInvalid"
    assert parsed.get("session_number") is None
    assert parsed["session_status"] is None


def test_health_payload_cannot_carry_a_session_number():
    health = healthy()
    parsed = contract.parse({**health, "session_number": 311}, "health")
    assert parsed["status"] == "blocked"
    assert parsed["error_code"] == "RemoteOutputInvalid"


# --- session error code extraction ------------------------------------------

def test_hostile_session_message_yields_only_the_oracle_code():
    code = service.extract_session_error_code(HOSTILE_ERROR)
    assert code == "ORA-00932"
    assert "INSERT" not in code
    assert "HR" not in code
    assert "O'Brien" not in code
    assert "NUMBER" not in code


@pytest.mark.parametrize("text, expected", [
    ("ODI-1217: Session error", "ODI-1217"),
    ("failed with ODI-12345 at step LOAD", "ODI-12345"),
    ("no oracle token here", None),
    ("ORA-932 is too short", None),
    ("ODI-12 is too short", None),
    (None, None),
    (12345, None),
    ({"ERROR_MESSAGE": HOSTILE_ERROR}, None),
])
def test_error_code_pattern_is_whole_token_ora5_or_odi4or5(text, expected):
    assert service.extract_session_error_code(text) == expected


# --- mapping_run_service: counts and ERROR code -----------------------------

def test_success_with_counts_adds_only_non_negative_integers(monkeypatch):
    calls = []

    def reader(session_number):
        calls.append(session_number)
        return dict(READER_ROW)

    outcome = _run(monkeypatch, with_session(done()), reader)
    assert outcome.status_code == 200
    assert outcome.payload["status"] == "SUCCESS"
    assert outcome.payload["session_number"] == 311
    assert outcome.payload["counts"] == COUNTS
    assert "session_error_code" not in outcome.payload
    assert outcome.payload["remote"]["status"] == "passed"
    assert outcome.payload["remote"]["session_status"] == "DONE"
    assert outcome.payload["remote"]["session_number"] == 311
    assert HOSTILE_ERROR not in json.dumps(outcome.payload)
    assert "ERROR_MESSAGE" not in json.dumps(outcome.payload)
    assert calls == [311]


@pytest.mark.parametrize("reader", [
    lambda _n: (_ for _ in ()).throw(RuntimeError("repository down")),
    lambda _n: None,
    lambda _n: {},
    lambda _n: {**READER_ROW, "PROCESSED_ROWS": 1.5},
    lambda _n: {**READER_ROW, "INSERTED_ROWS": "8"},
    lambda _n: {**READER_ROW, "UPDATED_ROWS": True},
    lambda _n: {**READER_ROW, "ERROR_ROWS": -1},
])
def test_success_without_usable_counts_equals_today_plus_session_only(monkeypatch, reader):
    baseline = _run(monkeypatch, done(), lambda _n: pytest.fail("reader reached without a session"))
    outcome = _run(monkeypatch, with_session(done()), reader)
    assert outcome.status_code == baseline.status_code == 200
    assert outcome.payload["status"] == baseline.payload["status"] == "SUCCESS"
    assert outcome.payload["remote"]["status"] == "passed"
    assert outcome.payload["remote"]["session_status"] == "DONE"
    assert outcome.payload["session_number"] == 311
    assert "counts" not in outcome.payload
    assert "session_error_code" not in outcome.payload
    assert _today_success_keys(outcome.payload) == _today_success_keys(baseline.payload)
    for key in ("scenario", "version", "context", "truncate_target_table"):
        assert outcome.payload[key] == baseline.payload[key]


def test_error_with_code_does_not_leak_message_sql_or_values(monkeypatch):
    outcome = _run(monkeypatch, with_session(failed_run()), lambda _n: dict(READER_ROW))
    body = json.dumps(outcome.payload)
    assert outcome.status_code == 502
    assert outcome.payload["status"] == "failed"
    assert outcome.payload["remote"]["error_code"] == "ScenarioRunFailed"
    assert outcome.payload["session_number"] == 311
    assert outcome.payload["session_error_code"] == "ORA-00932"
    assert outcome.payload["counts"] == COUNTS
    assert HOSTILE_ERROR not in body
    assert "INSERT INTO" not in body
    assert "O'Brien" not in body
    assert "HR.EMPLOYEES" not in body
    assert "ERROR_MESSAGE" not in body


def test_error_without_code_omits_the_field(monkeypatch):
    row = dict(READER_ROW)
    row["ERROR_MESSAGE"] = "step failed with no oracle token"
    outcome = _run(monkeypatch, with_session(failed_run()), lambda _n: row)
    assert outcome.status_code == 502
    assert outcome.payload["status"] == "failed"
    assert "session_error_code" not in outcome.payload
    assert outcome.payload["session_number"] == 311


def test_error_without_session_number_matches_today(monkeypatch):
    baseline = _run(monkeypatch, failed_run(), lambda _n: pytest.fail("reader reached"))
    outcome = _run(monkeypatch, failed_run(), lambda _n: pytest.fail("reader reached"))
    assert outcome.status_code == baseline.status_code == 502
    assert outcome.payload["status"] == "failed"
    assert "session_number" not in outcome.payload
    assert "counts" not in outcome.payload
    assert "session_error_code" not in outcome.payload
    assert set(outcome.payload) == set(baseline.payload)


def test_session_read_is_exactly_once_and_select_only(monkeypatch):
    calls = []

    def reader(session_number):
        calls.append(session_number)
        return dict(READER_ROW)

    _run(monkeypatch, with_session(done()), reader)
    assert calls == [311]
    source = (ROOT / "mapping_run_service.py").read_text(encoding="utf-8")
    assert source.count("_read_session_row(") >= 1
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source


# --- persisted state --------------------------------------------------------

def test_pre_change_state_file_still_loads():
    raw = json.dumps(PRE_CHANGE_STATE)
    loaded = json.loads(raw)
    persisted = state_module._persistable_state(loaded)
    assert persisted["mapping_name"] == "MAP_TEST"
    assert persisted["overall_status"] == "SUCCEEDED"
    assert "session_number" not in persisted
    assert "counts" not in persisted
    assert "session_error_code" not in persisted


def test_new_state_round_trips_session_counts_and_code():
    options = _options()
    evidence = {
        "session_number": 311,
        "counts": dict(COUNTS),
        "session_error_code": "ORA-00932",
    }
    state = service._state_payload(
        options, "RUN-NEWSTATE01", "MAP_TEST_SCN", "run_scenario", "RUN_FAILED",
        ["update_km", "generate_scenario"], "ScenarioRunFailed", evidence=evidence,
    )
    persisted = state_module._persistable_state(state)
    loaded = json.loads(json.dumps(persisted, default=state_module._json_default, sort_keys=True))
    assert loaded["session_number"] == 311
    assert loaded["counts"] == COUNTS
    assert loaded["session_error_code"] == "ORA-00932"
    assert HOSTILE_ERROR not in json.dumps(loaded)
    assert loaded["mapping_name"] == "MAP_TEST"


def test_persist_rejects_non_integer_counts_and_hostile_code():
    state = {
        **PRE_CHANGE_STATE,
        "session_number": 0,
        "counts": {"processed": 1, "inserted": 2, "updated": 3, "errors": -1, "sql": HOSTILE_ERROR},
        "session_error_code": HOSTILE_ERROR,
    }
    persisted = state_module._persistable_state(state)
    assert "session_number" not in persisted
    assert "counts" not in persisted
    assert persisted.get("session_error_code") != HOSTILE_ERROR
    assert HOSTILE_ERROR not in json.dumps(persisted)


# --- groovy text contract ---------------------------------------------------

def _runner_source() -> str:
    if not GROOVY.is_file():
        pytest.skip("run_scenario_remote.groovy is not in this tree")
    return GROOVY.read_text(encoding="utf-8")


def test_groovy_reports_session_id_fail_safe_as_session_number():
    source = _runner_source()
    assert "session_number" in source
    assert "getSessionId()" in source
    assert "getStatusMessage" not in source
    assert "getMessage(" not in source
    start = source.index("def info = invoker.invokeStartScenario")
    block = source[start:source.index("} catch (Exception ignored)", start)]
    assert "getSessionId()" in block
    assert "session_number" in block
    assert "catch (Exception" in source[start:]
    assert source.index("payload.session_number") > source.index("session_number: null") or (
        "session_number: null" in source
    )


def test_session_reader_reuses_odi_mcp_select_list():
    if not ODI_MCP.is_file():
        pytest.skip("odi_mcp.py is not in this tree")
    text = ODI_MCP.read_text(encoding="utf-8")
    assert "def read_odi_session(" in text
    assert "NB_ROW AS PROCESSED_ROWS" in text
    assert "NB_INS AS INSERTED_ROWS" in text
    assert "NB_UPD AS UPDATED_ROWS" in text
    assert "NB_ERR AS ERROR_ROWS" in text
    assert "SESS_MESS AS ERROR_MESSAGE" in text
    assert "WHERE SESS_NO = :sess_no" in text
    assert "INSERT " not in text.split("def read_odi_session")[1].split("@mcp.tool")[0]
