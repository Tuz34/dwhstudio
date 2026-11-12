"""ODI-91: session number, count-only evidence and a safe ERROR code.

Offline only. These tests must fail on the pre-change contract and service.
"""

from __future__ import annotations

import json
import logging
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
        def adapted(session_number, **_kwargs):
            return reader(session_number)

        monkeypatch.setattr(service, "_read_session_row", adapted)
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
    assert "def read_session_run_evidence(" in text
    body = text.split("def read_session_run_evidence", 1)[1].split("\n@", 1)[0]
    assert "NB_ROW AS PROCESSED_ROWS" in body
    assert "NB_INS AS INSERTED_ROWS" in body
    assert "NB_UPD AS UPDATED_ROWS" in body
    assert "NB_ERR AS ERROR_ROWS" in body
    assert "SUBSTR(SESS_MESS, 1, {_EVIDENCE_MESSAGE_CHARS})" in body
    assert "_EVIDENCE_MESSAGE_CHARS = 4000" in text
    assert "WHERE SESS_NO = :sess_no" in body
    assert "INSERT " not in body
    assert "UPDATE " not in body
    assert "DELETE " not in body


# --- round 2: review rulings ------------------------------------------------

_LIST_JSX = ROOT / "frontend" / "src" / "components" / "dataops" / "MappingList.jsx"
_PANEL_JS = ROOT / "frontend" / "src" / "components" / "dataops" / "mappingRunOutcome.js"
_ARABIC_CODE = "ORA-\u0660\u0660\u0669\u0663\u0662"
_UNSAFE_INTEGERS = (2**53, 10**5000)
_COUNT_COLUMNS = ("PROCESSED_ROWS", "INSERTED_ROWS", "UPDATED_ROWS", "ERROR_ROWS")


def _evidence_script(**overrides):
    script = {
        "sql": [],
        "params": [],
        "events": [],
        "fetchmany": [],
        "fetchall": 0,
        "rows": [(311, 10, 8, 2, 0)],
        "error_rows": [(311, HOSTILE_ERROR)],
        "closed": False,
    }
    script.update(overrides)
    return script


def _install_evidence_driver(monkeypatch, script):
    """Fake oracledb connection. Records driver limits and never sleeps."""
    import odi_mcp

    class Cursor:
        def __init__(self):
            self.prefetchrows = None
            self.last_sql = ""

        def execute(self, sql, params=None):
            self.last_sql = sql
            script["sql"].append(sql)
            script["params"].append(params)
            script["events"].append("execute")
            error = script.get("execute_error")
            if error is not None:
                raise error

        @property
        def description(self):
            if "SUBSTR" in self.last_sql:
                return [("SESS_NO",), ("ERROR_MESSAGE",)]
            return [("SESS_NO",), *((name,) for name in _COUNT_COLUMNS)]

        def _payload(self):
            if "SUBSTR" in self.last_sql:
                return script["error_rows"]
            return script["rows"]

        def fetchmany(self, size):
            script["fetchmany"].append(size)
            return list(self._payload())[:size]

        def fetchall(self):
            script["fetchall"] += 1
            return list(self._payload())

    class Connection:
        def __init__(self):
            self._call_timeout = 0

        @property
        def call_timeout(self):
            return self._call_timeout

        @call_timeout.setter
        def call_timeout(self, value):
            self._call_timeout = value
            script["call_timeout"] = value
            script["events"].append("call_timeout")

        def cursor(self):
            return Cursor()

        def close(self):
            script["closed"] = True

    def connect(*_args, **kwargs):
        script["tcp_connect_timeout"] = kwargs.get("tcp_connect_timeout", "missing")
        script["events"].append("connect")
        error = script.get("connect_error")
        if error is not None:
            raise error
        return Connection()

    monkeypatch.setattr(odi_mcp.oracledb, "connect", connect)


def _assert_budget(script, *, require_call_timeout):
    timeout = script.get("tcp_connect_timeout", "missing")
    assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
    assert 0 < timeout <= 5
    if require_call_timeout:
        call_ms = script.get("call_timeout", 0)
        assert isinstance(call_ms, int) and not isinstance(call_ms, bool)
        assert call_ms > 0
        assert timeout + 2 * (call_ms / 1000) <= 5


def test_round2_done_read_is_bounded_and_skips_the_message(monkeypatch):
    script = _evidence_script()
    _install_evidence_driver(monkeypatch, script)
    outcome = _run(monkeypatch, with_session(done()))
    _assert_budget(script, require_call_timeout=True)
    assert script["events"][:3] == ["connect", "call_timeout", "execute"]
    assert script["closed"] is True
    assert len(script["sql"]) == 1
    sql = script["sql"][0]
    assert "SESS_MESS" not in sql
    assert "SESS_NAME" not in sql
    assert "SESS_BEG" not in sql
    assert "SESS_END" not in sql
    assert "SESS_STATUS" not in sql
    for column in ("SESS_NO", "NB_ROW", "NB_INS", "NB_UPD", "NB_ERR"):
        assert column in sql
    assert script["fetchmany"] == [2]
    assert script["fetchall"] == 0
    assert script["params"] == [{"sess_no": 311}]
    assert outcome.status_code == 200
    assert outcome.payload["counts"] == COUNTS
    assert "session_error_code" not in outcome.payload
    legacy = ODI_MCP.read_text(encoding="utf-8").split("def execute_odi_lob_query", 1)[1]
    legacy = legacy.split("def ", 1)[0]
    assert "tcp_connect_timeout" not in legacy
    assert "call_timeout" not in legacy


def test_round2_failed_read_cuts_the_message_in_sql(monkeypatch):
    script = _evidence_script()
    _install_evidence_driver(monkeypatch, script)
    outcome = _run(monkeypatch, with_session(failed_run()))
    _assert_budget(script, require_call_timeout=True)
    assert len(script["sql"]) == 2
    assert "SESS_MESS" not in script["sql"][0]
    error_sql = script["sql"][1]
    assert "SUBSTR(SESS_MESS, 1, 4000)" in error_sql
    assert "SESS_NO" in error_sql
    assert "NB_ROW" not in error_sql
    assert script["fetchmany"] == [2, 2]
    assert outcome.status_code == 502
    assert outcome.payload["session_error_code"] == "ORA-00932"
    assert outcome.payload["counts"] == COUNTS
    assert HOSTILE_ERROR not in json.dumps(outcome.payload)


def test_round2_stalled_connect_is_evidence_absent(monkeypatch):
    script = _evidence_script(connect_error=TimeoutError("stalled connect"))
    _install_evidence_driver(monkeypatch, script)
    outcome = _run(monkeypatch, with_session(done()))
    _assert_budget(script, require_call_timeout=False)
    assert script["sql"] == []
    assert script.get("call_timeout") is None
    assert outcome.status_code == 200
    assert outcome.payload["status"] == "SUCCESS"
    assert outcome.payload["session_number"] == 311
    assert "counts" not in outcome.payload
    assert "session_error_code" not in outcome.payload


def test_round2_stalled_execute_is_evidence_absent(monkeypatch):
    script = _evidence_script(execute_error=TimeoutError("stalled execute"))
    _install_evidence_driver(monkeypatch, script)
    outcome = _run(monkeypatch, with_session(failed_run()))
    _assert_budget(script, require_call_timeout=True)
    assert script["closed"] is True
    assert outcome.status_code == 502
    assert outcome.payload["status"] == "failed"
    assert outcome.payload["remote"]["error_code"] == "ScenarioRunFailed"
    assert outcome.payload["session_number"] == 311
    assert "counts" not in outcome.payload
    assert "session_error_code" not in outcome.payload


@pytest.mark.parametrize("rows", [
    [],
    [(311, 1, 0, 0, 0), (311, 99, 0, 0, 0)],
    [(999, 10, 8, 2, 0)],
    [("311", 10, 8, 2, 0)],
])
def test_round2_row_must_be_exactly_one_matching_session(monkeypatch, rows):
    script = _evidence_script(rows=rows)
    _install_evidence_driver(monkeypatch, script)
    outcome = _run(monkeypatch, with_session(done()))
    assert outcome.status_code == 200
    assert outcome.payload["session_number"] == 311
    assert "counts" not in outcome.payload
    assert script["fetchmany"] == [2]
    assert script["fetchall"] == 0


def test_round2_mismatched_error_row_omits_only_the_code(monkeypatch):
    script = _evidence_script(error_rows=[(999, HOSTILE_ERROR)])
    _install_evidence_driver(monkeypatch, script)
    outcome = _run(monkeypatch, with_session(failed_run()))
    assert len(script["sql"]) == 2
    assert outcome.status_code == 502
    assert outcome.payload["counts"] == COUNTS
    assert "session_error_code" not in outcome.payload
    assert HOSTILE_ERROR not in json.dumps(outcome.payload)


@pytest.mark.parametrize("where,exc_type", [
    ("counts", LookupError),
    ("reader", Exception),
])
def test_round2_collector_exceptions_still_persist_and_alert(monkeypatch, caplog, where, exc_type):
    events = setup_pipeline(monkeypatch, run=with_session(failed_run()))
    monkeypatch.setattr(service, "_read_session_row", lambda *_a, **_k: dict(READER_ROW))
    if where == "counts":
        monkeypatch.setattr(
            service, "_counts_from_row", lambda _row: (_ for _ in ()).throw(exc_type("LEAKED-TOKEN")),
        )
    else:
        monkeypatch.setattr(
            service, "_read_session_row", lambda *_a, **_k: (_ for _ in ()).throw(exc_type("LEAKED-TOKEN")),
        )
    with caplog.at_level(logging.WARNING, logger="mapping_run_service"):
        outcome = service.execute_mapping_run(Mock(), _options())
    assert events == ["health", "options", "generate", "run", "persist", "alert"]
    assert outcome.status_code == 502
    assert outcome.payload["status"] == "failed"
    assert outcome.payload["remote"]["error_code"] == "ScenarioRunFailed"
    assert outcome.payload["session_number"] == 311
    assert "counts" not in outcome.payload
    assert "session_error_code" not in outcome.payload
    assert [record.message for record in caplog.records] == [exc_type.__name__]
    assert "LEAKED-TOKEN" not in caplog.text


def test_round2_error_code_rejects_non_ascii_digits():
    assert service.extract_session_error_code(f"failed {_ARABIC_CODE} after") is None
    persisted = state_module._persistable_state({**PRE_CHANGE_STATE, "session_error_code": _ARABIC_CODE})
    assert "session_error_code" not in persisted
    shape = "ORA-[0-9]{5}|ODI-[0-9]{4,5}"
    service_src = (ROOT / "mapping_run_service.py").read_text(encoding="utf-8")
    state_src = (ROOT / "mapping_state_manager.py").read_text(encoding="utf-8")
    panel_src = _PANEL_JS.read_text(encoding="utf-8")
    assert shape in service_src
    assert shape in state_src
    assert shape in panel_src


def test_round2_unsafe_integers_are_rejected_at_each_layer(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_read_session_row", lambda *args, **_kwargs: calls.append(args) or dict(READER_ROW))
    for number in _UNSAFE_INTEGERS:
        parsed = contract.parse(with_session(done(), number), "run")
        assert parsed["status"] == "unknown"
        assert parsed["error_code"] == "RemoteOutputInvalid"
        assert parsed.get("session_number") is None
        calls.clear()
        assert service._collect_run_evidence({"session_number": number}) == {}
        assert calls == []
        for column in _COUNT_COLUMNS:
            row = dict(READER_ROW)
            row[column] = number
            assert service._counts_from_row(row) is None
        for key in ("processed", "inserted", "updated", "errors"):
            counts = dict(COUNTS)
            counts[key] = number
            persisted = state_module._persistable_state({
                **PRE_CHANGE_STATE, "session_number": number, "counts": counts,
            })
            assert "session_number" not in persisted
            assert "counts" not in persisted
    parsed_raw = contract.parse(json.dumps(with_session(done(), 2**53)), "run")
    assert parsed_raw["status"] == "unknown"
    assert parsed_raw["error_code"] == "RemoteOutputInvalid"
    accepted = contract.parse(with_session(done(), 2**53 - 1), "run")
    assert accepted["status"] == "passed"
    assert accepted["session_number"] == 2**53 - 1
    max_row = dict(READER_ROW)
    max_row["PROCESSED_ROWS"] = 2**53 - 1
    assert service._counts_from_row(max_row)["processed"] == 2**53 - 1
    kept = state_module._persistable_state({
        **PRE_CHANGE_STATE,
        "session_number": 2**53 - 1,
        "counts": {**COUNTS, "processed": 2**53 - 1},
    })
    assert kept["session_number"] == 2**53 - 1
    assert kept["counts"]["processed"] == 2**53 - 1


def test_round2_refresh_shows_loading_and_clears_a_missing_selection():
    """Source contract: MappingList.jsx is not mounted in these tests."""
    content = _LIST_JSX.read_text(encoding="utf-8")
    assert "setLoading(true)" not in content
    assert "const [loading, setLoading] = useState(true);" in content
    assert "setRefreshing(true)" in content
    assert "Refreshing..." in content
    assert "aria-busy={refreshing}" in content
    placeholder, layout = content.split("{loading ?", 1)[1].split(") : (", 1)
    assert "Loading..." in placeholder
    assert "overflow-y-auto" not in placeholder
    assert "flex-1 overflow-y-auto custom-scrollbar space-y-6" in layout
    start = content.index("const loadMappings = useCallback")
    body = content[start:content.index("}, [loadDetail, showToast]);", start)]
    assert "selectedNameRef.current" in body
    assert "setDetail(null)" in body
    assert "setRunDialog" in body
    assert "setLoading(true)" not in body
