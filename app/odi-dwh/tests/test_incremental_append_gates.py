"""Finding #4: post-load gates are computed server-side, never caller-asserted."""

import json

import pytest

import watermark_manager
from tests.test_incremental_strategy import (
    FakeAppendConnection,
    FakeIncrementalConnection,
    FakeWatermarkWriter,
)

FIXED_MESSAGE = "Post-load gate results are computed server-side and cannot be supplied by the caller."
COLUMNS = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]


def _ready_preflight_report():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source = FakeIncrementalConnection(
        tables={"SH.SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}, {"PROD_ID": 2, "TIME_ID": "2026-06-20"}]},
        columns={"SH.SALES": COLUMNS},
    )
    target = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": COLUMNS},
    )
    return watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source, target_conn=target)


class _Recorder:
    def __init__(self, golden="passed", dq="passed"):
        self.golden = golden
        self.dq = dq
        self.golden_calls = []
        self.dq_calls = []

    def install(self, monkeypatch):
        def fake_golden(specs, *, source_conn, target_conn, row_limit=None):
            self.golden_calls.append(
                {"specs": list(specs), "source_conn": source_conn, "target_conn": target_conn, "row_limit": row_limit}
            )
            return {"status": self.golden, "results": [{"key_differences": [{"key": "LEAK-ME"}]}]}

        def fake_dq(specs, *, conn):
            self.dq_calls.append({"specs": list(specs), "conn": conn})
            return {"status": self.dq, "results": []}

        monkeypatch.setattr(watermark_manager, "run_live_regressions", fake_golden)
        monkeypatch.setattr(watermark_manager, "run_post_load_dq_gate", fake_dq)


@pytest.mark.parametrize(
    "extra",
    [
        {"post_load_gate_results": {"post_load_golden_record_passed": True, "post_load_dq_passed": True}},
        {"post_load_gate_results": {}},
        {"post_load_golden_record_passed": True},
        {"post_load_dq_passed": True},
    ],
)
def test_route_rejects_caller_asserted_gate_flags_with_fixed_message(app_client, monkeypatch, extra):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "run_configured_incremental_strategy_preflight",
        lambda _specs: pytest.fail("preflight must not run when gate flags are supplied"),
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "_open_incremental_target_connection",
        lambda: pytest.fail("write connection must not open"),
        raising=False,
    )

    resp = app_client.post("/api/incremental/execute", json={"spec_name": "SH_SALES_ODS", "approved": True, **extra})

    assert resp.status_code == 400
    assert resp.get_json() == {"error": FIXED_MESSAGE}


def test_route_without_flags_runs_executor_which_computes_gates(app_client, monkeypatch):
    import app as app_module
    from routes import schema_write

    class Conn:
        def close(self):
            pass

    class WM:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    seen = {}

    def fake_execute(preflight_result, *, approved, target_conn, source_conn, watermark_writer):
        seen["approved"] = approved
        return {
            "status": "executed",
            "post_load_gates": {"post_load_golden_record_passed": True, "post_load_dq_passed": True},
        }

    report = _ready_preflight_report()
    monkeypatch.setattr(app_module, "run_configured_incremental_strategy_preflight", lambda _s: report, raising=False)
    monkeypatch.setattr(app_module, "_open_incremental_target_connection", lambda: Conn(), raising=False)
    monkeypatch.setattr(schema_write, "_open_incremental_source_connection", lambda: Conn(), raising=False)
    monkeypatch.setattr(app_module, "WatermarkManager", WM)
    monkeypatch.setattr(app_module, "execute_incremental_append", fake_execute, raising=False)

    resp = app_client.post("/api/incremental/execute", json={"spec_name": "SH_SALES_ODS", "approved": True})

    assert resp.status_code == 200
    assert seen["approved"] is True
    assert resp.get_json()["execution"]["post_load_gates"] == {
        "post_load_golden_record_passed": True,
        "post_load_dq_passed": True,
    }


def test_gates_run_server_side_on_the_same_connection_as_the_append(monkeypatch):
    recorder = _Recorder()
    recorder.install(monkeypatch)
    target = FakeAppendConnection(rowcount=1)
    source = object()
    writer = FakeWatermarkWriter()

    result = watermark_manager.execute_incremental_append(
        _ready_preflight_report()["results"][0],
        approved=True,
        target_conn=target,
        source_conn=source,
        watermark_writer=writer,
    )

    assert result["status"] == "executed"
    assert len(recorder.golden_calls) == 1 and len(recorder.dq_calls) == 1
    golden = recorder.golden_calls[0]
    assert golden["target_conn"] is target
    assert golden["source_conn"] is source
    assert golden["row_limit"] is None
    assert [spec.source_table for spec in golden["specs"]] == ["SH.SALES"]
    assert [spec.target_table for spec in golden["specs"]] == ["DWH.ODS_SH_SALES"]
    assert recorder.dq_calls[0]["conn"] is target
    assert [spec.name for spec in recorder.dq_calls[0]["specs"]] == ["SH_SALES_ODS"]
    assert target.executed and target.commits == 1


@pytest.mark.parametrize(
    ("golden", "dq", "expected_failed"),
    [
        ("attention_required", "passed", ["post_load_golden_record_passed"]),
        ("passed", "failed", ["post_load_dq_passed"]),
        ("attention_required", "failed", ["post_load_golden_record_passed", "post_load_dq_passed"]),
    ],
)
def test_gate_failure_rolls_back_and_leaves_watermark_unchanged(monkeypatch, golden, dq, expected_failed):
    _Recorder(golden=golden, dq=dq).install(monkeypatch)
    target = FakeAppendConnection(rowcount=1)
    writer = FakeWatermarkWriter()

    result = watermark_manager.execute_incremental_append(
        _ready_preflight_report()["results"][0],
        approved=True,
        target_conn=target,
        source_conn=object(),
        watermark_writer=writer,
    )

    assert result["status"] == "rolled_back_post_load_gate_failed"
    assert result["failed_gates"] == expected_failed
    assert result["rows_appended"] == 0
    assert result["watermark_update_attempted"] is False
    assert target.commits == 0 and target.rollbacks == 1
    assert writer.calls == []
    dumped = json.dumps(result, default=str)
    assert "LEAK-ME" not in dumped
    assert "key_differences" not in dumped


def test_gate_database_error_fails_closed_without_leaking_details(monkeypatch):
    import oracledb

    def boom(*_a, **_k):
        raise oracledb.DatabaseError("internal-dsn-detail")

    monkeypatch.setattr(watermark_manager, "run_live_regressions", boom)
    monkeypatch.setattr(watermark_manager, "run_post_load_dq_gate", lambda specs, **_k: {"status": "passed"})
    target = FakeAppendConnection(rowcount=1)
    writer = FakeWatermarkWriter()

    result = watermark_manager.execute_incremental_append(
        _ready_preflight_report()["results"][0],
        approved=True,
        target_conn=target,
        source_conn=object(),
        watermark_writer=writer,
    )

    assert result["status"] == "rolled_back_post_load_gate_failed"
    assert result["failed_gates"] == ["post_load_golden_record_passed"]
    assert "internal-dsn-detail" not in json.dumps(result, default=str)
    assert target.commits == 0 and writer.calls == []


def test_executor_no_longer_accepts_caller_supplied_gate_results():
    with pytest.raises(TypeError):
        watermark_manager.execute_incremental_append(
            _ready_preflight_report()["results"][0],
            approved=True,
            target_conn=FakeAppendConnection(),
            source_conn=object(),
            watermark_writer=FakeWatermarkWriter(),
            post_load_gate_results={"post_load_golden_record_passed": True, "post_load_dq_passed": True},
        )


def test_unexpected_gate_error_is_not_a_gate_failure_and_rolls_back(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("programming error")

    monkeypatch.setattr(watermark_manager, "run_live_regressions", boom)
    monkeypatch.setattr(watermark_manager, "run_post_load_dq_gate", lambda specs, **_k: {"status": "passed"})
    target = FakeAppendConnection(rowcount=1)
    writer = FakeWatermarkWriter()

    with pytest.raises(RuntimeError):
        watermark_manager.execute_incremental_append(
            _ready_preflight_report()["results"][0],
            approved=True,
            target_conn=target,
            source_conn=object(),
            watermark_writer=writer,
        )

    assert target.commits == 0 and target.rollbacks == 1
    assert writer.calls == []


@pytest.mark.parametrize("exc", [ValueError("secret detail"), RuntimeError("secret detail"), TypeError("secret detail")])
def test_route_maps_any_unexpected_executor_error_to_redacted_500(app_client, monkeypatch, exc):
    import app as app_module
    from routes import schema_write

    class Conn:
        def close(self):
            pass

    class WM:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def boom(*_a, **_k):
        raise exc

    report = _ready_preflight_report()
    monkeypatch.setattr(app_module, "run_configured_incremental_strategy_preflight", lambda _s: report, raising=False)
    monkeypatch.setattr(app_module, "_open_incremental_target_connection", lambda: Conn(), raising=False)
    monkeypatch.setattr(schema_write, "_open_incremental_source_connection", lambda: Conn(), raising=False)
    monkeypatch.setattr(app_module, "WatermarkManager", WM)
    monkeypatch.setattr(app_module, "execute_incremental_append", boom, raising=False)

    resp = app_client.post("/api/incremental/execute", json={"spec_name": "SH_SALES_ODS", "approved": True})

    assert resp.status_code == 500
    body = json.dumps(resp.get_json())
    assert "secret detail" not in body
    assert resp.get_json()["error"] == "Incremental execution failed."
