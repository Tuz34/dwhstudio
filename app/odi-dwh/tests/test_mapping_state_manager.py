import json
from datetime import datetime, timedelta


class _Clob:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or {}))

    def fetchall(self):
        return self.rows

    def close(self):
        return None


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)

    def is_healthy(self):
        return True

    def cursor(self):
        return self.cursor_obj


def test_list_recent_runs_returns_redacted_newest_run_shape(monkeypatch):
    from mapping_state_manager import MappingStateManager

    created = datetime(2026, 6, 18, 19, 44, 0)
    updated = created + timedelta(seconds=7)
    state = {
        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN",
        "scenario_version": "001",
        "context": "GLOBAL",
        "truncate_target_table": True,
        "overall_status": "RUN_FAILED",
        "current_step": "run_scenario",
        "error": "FATAL password=secret jdbc:oracle:thin:@host/service raw detail",
    }
    conn = _Conn([
        (
            "RUN-001",
            "MAP_HR_EMPLOYEES_TO_ODS",
            "run_scenario",
            "RUN_FAILED",
            "FATAL password=secret jdbc:oracle:thin:@host/service raw detail",
            _Clob(json.dumps(state)),
            created,
            updated,
        )
    ])
    manager = MappingStateManager()
    monkeypatch.setattr(manager, "_get_conn", lambda: conn)

    runs = manager.list_recent_runs(limit=10)

    assert len(runs) == 1
    run = runs[0]
    assert run["batch_id"] == "RUN-001"
    assert run["mapping_name"] == "MAP_HR_EMPLOYEES_TO_ODS"
    assert run["scenario_name"] == "MAP_HR_EMPLOYEES_TO_ODS_SCN"
    assert run["scenario_version"] == "001"
    assert run["overall_status"] == "RUN_FAILED"
    assert run["current_step"] == "run_scenario"
    assert run["truncate_target_table"] is True
    assert run["started_at"] == "2026-06-18T19:44:00"
    assert run["updated_at"] == "2026-06-18T19:44:07"
    assert run["duration_seconds"] == 7
    assert "password=secret" not in run["error"].lower()
    assert "jdbc:oracle:thin" not in run["error"].lower()


def test_list_recent_runs_filters_status_and_bounds_limit(monkeypatch):
    from mapping_state_manager import MappingStateManager

    conn = _Conn([])
    manager = MappingStateManager()
    monkeypatch.setattr(manager, "_get_conn", lambda: conn)

    runs = manager.list_recent_runs(limit=500, status="run_failed")

    assert runs == []
    sql, params = conn.cursor_obj.executed[0]
    assert "UPPER(OVERALL_STATUS) = :status" in sql
    assert params["status"] == "RUN_FAILED"
    assert params["lim"] == 100
