"""ODI-91/92: a write that was attempted must not come back as an empty failure.

Mocked and offline only. These tests lock the response bytes for exceptions that
happen before any write, and the explicit outcome once a write has been attempted.
"""

from __future__ import annotations

import oracledb
import pytest

import remote_run_contract
import scd2_builder
import scd2_service
from tests.scd2_support import REQUEST, SOURCE_COLUMNS, make_services, plan_catalog_rows, probe_payload
from tests.test_remote_run_contract import RemoteFakeOrchestrator, done, generated, healthy
from tests.test_scd2_service import RUN_BODY, ddl_handler, existing_probe

LEAK = "SYNTHETIC_LEAK_TOKEN"


def _pipeline(run_result):
    class Pipeline:
        def __init__(self):
            self.calls = []

        def remote_agent_health(self, **_kwargs):
            self.calls.append("health")
            return healthy()

        def update_mapping(self, **_kwargs):
            self.calls.append("options")
            return "SUCCESS: options updated"

        def generate_scenario_metadata(self, **kwargs):
            self.calls.append("generate")
            return generated(kwargs.get("scenario_name", "MAP_TEST_SCN"), kwargs.get("version", "001"))

        def run_scenario_remote(self, **_kwargs):
            self.calls.append("run")
            value = run_result() if callable(run_result) else run_result
            return value

    return Pipeline()


def _quiet_state():
    class Manager:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def upsert_state(self, *_args, **_kwargs):
            return None

        def create_alert(self, **_kwargs):
            return None

    return Manager


def _run_body():
    return {
        "mapping_name": "MAP_TEST",
        "truncate_target_table": False,
        "scenario_version": "001",
        "context": "GLOBAL",
        "approved": True,
    }


def _post(app_client, monkeypatch, pipeline, state_manager=None):
    import app as app_module
    import odi_orchestrator

    monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", lambda: pipeline)
    monkeypatch.setattr(app_module, "MappingStateManager", state_manager or _quiet_state())
    monkeypatch.setattr(app_module, "AlertManager", _quiet_state())
    return app_client.post("/api/mapping/run", json=_run_body())


def _assert_no_leak(response, caplog):
    assert LEAK not in response.get_data(as_text=True)
    assert LEAK not in caplog.text


def _assert_our_logs_hide_the_token(response, caplog):
    """Flask's own traceback log is pre-existing. Responses and our logger stay clean."""
    assert LEAK not in response.get_data(as_text=True)
    ours = [record.getMessage() for record in caplog.records if record.name == "dwh_app"]
    assert ours
    assert all(LEAK not in message for message in ours)


def test_remote_done_then_post_run_exception_keeps_success(app_client, monkeypatch, caplog):
    import app as app_module

    def explode(*_args, **_kwargs):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(app_module, "redact_sensitive_text", explode)
    response = _post(app_client, monkeypatch, _pipeline(done()))
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "SUCCESS"
    assert body["batch_id"].startswith("RUN-")
    assert body["scenario"] == "MAP_TEST_SCN"
    assert body["remote"]["status"] == "passed"
    assert body["remote"]["session_status"] == "DONE"
    assert body["remote"]["run_may_have_started"] is True
    assert "state_persist_failed" not in body
    assert "warning" not in body
    assert body.get("error") != "mapping run failed."
    _assert_no_leak(response, caplog)


def test_remote_unknown_then_post_run_exception_keeps_unknown(app_client, monkeypatch, caplog):
    import app as app_module

    def explode(*_args, **_kwargs):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(app_module, "redact_sensitive_text", explode)
    remote = remote_run_contract.transport_failure("run", "RemoteTimeout")
    response = _post(app_client, monkeypatch, _pipeline(remote))
    body = response.get_json()
    assert response.status_code == 504
    assert body["status"] == "unknown"
    assert body["batch_id"].startswith("RUN-")
    assert body["remote"]["outcome_unknown"] is True
    assert body["remote"]["run_may_have_started"] is True
    assert "state_persist_failed" not in body
    assert "warning" not in body
    assert body.get("error") != "mapping run failed."
    _assert_no_leak(response, caplog)


def test_persist_failure_is_reported_on_the_known_result(app_client, monkeypatch, caplog):
    import oracledb

    class Broken(_quiet_state()):
        def upsert_state(self, *_args, **_kwargs):
            raise oracledb.DatabaseError("audit write failed")

    response = _post(app_client, monkeypatch, _pipeline(done()), state_manager=Broken)
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "SUCCESS"
    assert body["batch_id"].startswith("RUN-")
    assert body["remote"]["session_status"] == "DONE"
    assert body["state_persist_failed"] is True
    assert body["warning"] == "The run completed but its history record could not be saved."
    assert "error" not in body
    assert "audit write failed" not in response.get_data(as_text=True)
    _assert_no_leak(response, caplog)


def test_remote_call_exception_is_explicit_unknown(app_client, monkeypatch, caplog):
    def explode(**_kwargs):
        raise RuntimeError(LEAK)

    pipeline = _pipeline(done())
    pipeline.run_scenario_remote = explode
    response = _post(app_client, monkeypatch, pipeline)
    body = response.get_json()
    assert response.status_code == 502
    assert body["status"] == "unknown"
    assert body["batch_id"].startswith("RUN-")
    assert body["run_may_have_started"] is True
    assert body["outcome_unknown"] is True
    assert body.get("error") != "mapping run failed."
    assert pipeline.calls == ["health", "options", "generate"]
    _assert_no_leak(response, caplog)


def test_mapping_run_pre_write_exception_response_is_unchanged(app_client, monkeypatch, caplog):
    pipeline = _pipeline(done())

    def explode(**_kwargs):
        raise RuntimeError(LEAK)

    pipeline.remote_agent_health = explode
    response = _post(app_client, monkeypatch, pipeline)
    assert response.status_code == 500
    assert response.get_json() == {"error": "mapping run failed.", "error_type": "RuntimeError"}
    assert LEAK not in response.get_data(as_text=True)
    assert LEAK not in caplog.text


def test_mapping_run_success_fields_unchanged_when_persist_succeeds(app_client, monkeypatch):
    response = _post(app_client, monkeypatch, _pipeline(done()))
    body = response.get_json()
    assert response.status_code == 200
    assert set(body) == {
        "status", "batch_id", "scenario", "version", "context", "truncate_target_table", "remote",
    }
    assert body["status"] == "SUCCESS"
    assert body["remote"] == done()


def test_partial_grants_require_known_side_effects_and_review(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)
    monkeypatch.setattr(config, "ODI_EXEC_USER", "ODI_EXEC")
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(
            RemoteFakeOrchestrator(plan, responses={"scd2_probe.groovy": existing_probe(plan)}),
            ddl_handler=ddl_handler(
                plan, table_exists=1, sequence_exists=1, grants=[], fail_on=plan["surrogate_key"]["sequence"],
            ),
        ),
        approved=True,
    )
    step = [item for item in payload["steps"] if item["step"] == "target_ddl"][0]
    assert status == 409
    assert payload["status"] == "blocked_target_incomplete"
    assert step["grants_applied"] == 4
    assert step["grants_failed"] == 1
    assert payload["known_side_effects"] is True
    assert payload["manual_review_required"] is True
    assert payload["outcome_unknown"] is False


def test_grants_applied_is_the_only_counter_that_requires_review():
    payload, status = scd2_service._generate_response(
        "blocked_target_incomplete", None, [{"step": "target_ddl", "grants_applied": 1}], 409,
    )
    assert status == 409
    assert payload["known_side_effects"] is True
    assert payload["manual_review_required"] is True
    assert payload["outcome_unknown"] is False
    for key in ("rows_changed", "objects_created", "column_count"):
        untouched, _status = scd2_service._generate_response(
            "blocked_target_incomplete", None, [{"step": "target_ddl", key: 4}], 409,
        )
        assert untouched["known_side_effects"] is False
        assert untouched["manual_review_required"] is False
        assert untouched["outcome_unknown"] is False


def test_read_counts_and_zero_applied_do_not_force_review():
    payload, _status = scd2_service._generate_response(
        "blocked_target_incomplete",
        None,
        [{
            "grants_applied": 0,
            "grants_failed": 5,
            "column_count": 12,
            "missing_grantee_count": 1,
            "source_column_count": 4,
            "target_created": False,
            "sequence_created": False,
        }],
        409,
    )
    assert payload["known_side_effects"] is False
    assert payload["manual_review_required"] is False
    assert payload["outcome_unknown"] is False


def test_successful_generation_keeps_flag_only_side_effects():
    payload, status = scd2_service._generate_response(
        "generated",
        None,
        [{"grants_applied": 4, "side_effects": False, "target_created": False, "sequence_created": False}],
        200,
    )
    assert status == 200
    assert payload["known_side_effects"] is False
    assert payload["manual_review_required"] is False
    assert payload["outcome_unknown"] is False


def _ddl_then_raise(plan):
    grants = {"n": 0}

    def handler(sql, params):
        text = " ".join(str(sql).split())
        if text.startswith(("CREATE SEQUENCE", "CREATE TABLE")):
            return []
        if text.startswith("GRANT"):
            grants["n"] += 1
            if grants["n"] >= 2:
                raise RuntimeError(LEAK)
            return []
        if "FROM ALL_USERS" in text:
            return [(1,)]
        if "FROM ALL_TABLES" in text or "FROM ALL_SEQUENCES" in text:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in text:
            return plan_catalog_rows(plan)
        return []

    return handler


def test_generate_exception_after_ddl_is_outcome_unknown(app_client, monkeypatch, caplog):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)
    monkeypatch.setattr(config, "ODI_EXEC_USER", "ODI_EXEC")
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan)
    services = make_services(orchestrator, ddl_handler=_ddl_then_raise(plan))
    monkeypatch.setattr(scd2_service, "default_services", lambda: services)
    response = app_client.post("/api/scd2/generate", json={**REQUEST, "approved": True})
    body = response.get_json()
    assert response.status_code == 500
    assert body["status"] == "outcome_unknown"
    assert body["outcome_unknown"] is True
    assert body["manual_review_required"] is True
    assert body["error"] == "SCD2 generation failed."
    assert body["error_type"] == "RuntimeError"
    assert LEAK not in response.get_data(as_text=True)
    assert LEAK not in caplog.text
    assert any(call[0] == "groovy" for call in orchestrator.calls)


def test_run_exception_after_remote_call_is_outcome_unknown(app_client, monkeypatch, caplog):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(
        plan, responses={"scd2_probe.groovy": probe_payload(plan, mapping_exists=True, scenario_exists=True)},
    )

    def catalog(sql, _params):
        if "FROM ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(plan)
        return [(0,)]

    calls = {"n": 0}

    def collect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError(LEAK)
        return {"status": "ok", "stock_contract_valid": True, "source_current_count_equal": True}

    monkeypatch.setattr(scd2_service.scd2_postflight, "collect_stock_postflight", collect)
    services = make_services(orchestrator, catalog_handler=catalog)
    monkeypatch.setattr(scd2_service, "default_services", lambda: services)
    import config
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)
    response = app_client.post("/api/scd2/run", json={**RUN_BODY, "approved": True})
    body = response.get_json()
    assert response.status_code == 500
    assert body["status"] == "outcome_unknown"
    assert body["outcome_unknown"] is True
    assert body["manual_review_required"] is True
    assert body["error"] == "SCD2 scenario run failed."
    assert body["error_type"] == "RuntimeError"
    assert any(call[0] == "scenario_run" for call in orchestrator.calls)
    assert LEAK not in response.get_data(as_text=True)
    assert LEAK not in caplog.text


def test_generate_pre_write_exception_response_is_unchanged(app_client, monkeypatch, caplog):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan)

    def boom(script, params):
        raise RuntimeError(LEAK)

    orchestrator.run_groovy = boom
    monkeypatch.setattr(scd2_service, "default_services", lambda: make_services(orchestrator))
    response = app_client.post("/api/scd2/generate", json={**REQUEST, "approved": True})
    assert response.status_code == 500
    assert response.get_json() == {"error": "SCD2 generation failed.", "error_type": "RuntimeError"}
    assert LEAK not in response.get_data(as_text=True)
    assert LEAK not in caplog.text


def test_run_pre_write_exception_response_is_unchanged(app_client, monkeypatch, caplog):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan)

    def boom(**_kwargs):
        raise RuntimeError(LEAK)

    orchestrator.remote_agent_health = boom
    monkeypatch.setattr(scd2_service, "default_services", lambda: make_services(orchestrator))
    response = app_client.post("/api/scd2/run", json={**RUN_BODY, "approved": True})
    assert response.status_code == 500
    assert response.get_json() == {"error": "SCD2 scenario run failed.", "error_type": "RuntimeError"}
    assert LEAK not in response.get_data(as_text=True)
    assert LEAK not in caplog.text


COMPLETED_WARNING = "The run completed but its history record could not be saved."
FAILED_WARNING = "The run failed but its history record could not be saved."
BLOCKED_WARNING = "The run was blocked but its history record could not be saved."
UNKNOWN_WARNING = "The run outcome is unknown but its history record could not be saved."


@pytest.fixture
def captured_errors():
    """Let the global handler run. The test client otherwise re-raises."""
    import app as app_module

    previous = app_module.app.config.get("PROPAGATE_EXCEPTIONS")
    app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        yield
    finally:
        if previous is None:
            app_module.app.config.pop("PROPAGATE_EXCEPTIONS", None)
        else:
            app_module.app.config["PROPAGATE_EXCEPTIONS"] = previous


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        if self.conn.fail == "merge":
            raise oracledb.DatabaseError("merge failed")
        self.conn.statements.append((sql, params))

    def close(self):
        if self.conn.fail == "cursor":
            raise OSError("cursor close failed")

    def fetchone(self):
        return (1,)


class _Conn:
    def __init__(self, fail):
        self.fail = fail
        self.statements = []
        self.committed = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        if self.fail == "commit":
            raise oracledb.DatabaseError("commit failed")
        self.committed = True

    def close(self):
        if self.fail == "connection":
            raise OSError("connection close failed")

    def is_healthy(self):
        return True


def _audit_manager(fail):
    import mapping_state_manager as state_module

    class Manager(state_module.MappingStateManager):
        last = None

        def __init__(self):
            super().__init__()
            self.audit = _Conn(fail)
            self.conn = self.audit
            Manager.last = self

        def ensure_table(self):
            return None

    return Manager


def _silence_rag(monkeypatch):
    import mapping_state_manager as state_module

    monkeypatch.setattr(state_module, "_push_success_to_rag", lambda *_a, **_k: None)
    monkeypatch.setattr(state_module, "_push_failure_to_rag", lambda *_a, **_k: None)


def _failed_remote():
    return remote_run_contract.result(
        "run", status="failed", stage="run", error_code="ScenarioRunFailed",
        agent=True, repository=True, may_have_started=True, session_status="ERROR",
    )


def _blocked_remote():
    return remote_run_contract.result(
        "run", status="blocked", stage="run", error_code="ScenarioRunFailed",
        agent=True, repository=True,
    )


@pytest.mark.parametrize("fail", ["merge", "commit"])
def test_uncommitted_merge_or_commit_sets_persist_warning(app_client, monkeypatch, fail):
    _silence_rag(monkeypatch)
    manager = _audit_manager(fail)
    response = _post(app_client, monkeypatch, _pipeline(done()), state_manager=manager)
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "SUCCESS"
    assert body["state_persist_failed"] is True
    assert body["warning"] == COMPLETED_WARNING
    assert "merge failed" not in response.get_data(as_text=True)
    assert "commit failed" not in response.get_data(as_text=True)
    assert manager.last.audit.committed is False


@pytest.mark.parametrize(
    ("remote", "http_status", "status", "warning"),
    [
        (_failed_remote(), 502, "failed", FAILED_WARNING),
        (_blocked_remote(), 502, "blocked", BLOCKED_WARNING),
        (remote_run_contract.transport_failure("run", "RemoteTimeout"), 504, "unknown", UNKNOWN_WARNING),
    ],
)
def test_uncommitted_audit_write_warns_on_every_known_shape(
    app_client, monkeypatch, remote, http_status, status, warning,
):
    _silence_rag(monkeypatch)
    response = _post(app_client, monkeypatch, _pipeline(remote), state_manager=_audit_manager("commit"))
    body = response.get_json()
    assert response.status_code == http_status
    assert body["status"] == status
    assert body["state_persist_failed"] is True
    assert body["warning"] == warning
    assert body["batch_id"].startswith("RUN-")


@pytest.mark.parametrize("fail", ["cursor", "connection"])
def test_close_after_commit_is_not_a_persist_failure(app_client, monkeypatch, fail):
    _silence_rag(monkeypatch)
    manager = _audit_manager(fail)
    response = _post(app_client, monkeypatch, _pipeline(done()), state_manager=manager)
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "SUCCESS"
    assert "state_persist_failed" not in body
    assert "warning" not in body
    conn = manager.last.audit
    assert conn.committed is True
    written = conn.statements[-1][1]
    assert written["batch_id"] == body["batch_id"]
    assert written["overall_status"] == "SUCCEEDED"
    assert body["batch_id"] in written["state_json"]
    assert "SUCCEEDED" in written["state_json"]


def test_key_error_while_building_state_keeps_the_outcome(app_client, monkeypatch, caplog):
    import mapping_run_service as service

    def explode(*_args, **_kwargs):
        raise KeyError(LEAK)

    monkeypatch.setattr(service, "_state_payload", explode)
    response = _post(app_client, monkeypatch, _pipeline(done()))
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "SUCCESS"
    assert body["batch_id"].startswith("RUN-")
    assert body["remote"]["session_status"] == "DONE"
    assert body["remote"]["run_may_have_started"] is True
    assert "state_persist_failed" not in body
    assert "warning" not in body
    _assert_no_leak(response, caplog)


@pytest.mark.parametrize("method", ["update_mapping", "generate_scenario_metadata"])
def test_repository_write_exception_names_the_unknown_repository_outcome(
    app_client, monkeypatch, caplog, method,
):
    pipeline = _pipeline(done())

    def explode(**_kwargs):
        raise RuntimeError(LEAK)

    setattr(pipeline, method, explode)
    response = _post(app_client, monkeypatch, pipeline)
    body = response.get_json()
    assert response.status_code == 500
    assert body == {
        "batch_id": body["batch_id"],
        "status": "repository_outcome_unknown",
        "repository_write_may_have_happened": True,
        "run_may_have_started": False,
        "outcome_unknown": True,
        "error": "mapping run failed.",
        "error_type": "RuntimeError",
    }
    assert body["batch_id"].startswith("RUN-")
    assert "run" not in pipeline.calls
    _assert_no_leak(response, caplog)


def test_rejected_repository_results_stay_unchanged(app_client, monkeypatch):
    denied = _pipeline(done())
    denied.update_mapping = lambda **_kwargs: "FAILED: options"
    denied_response = _post(app_client, monkeypatch, denied)
    assert denied_response.status_code == 500
    denied_body = denied_response.get_json()
    assert set(denied_body) == {"error", "batch_id"}
    assert denied_body["error"] == "Scenario option update failed."

    unready = _pipeline(done())
    unready.generate_scenario_metadata = lambda **_kwargs: {"status": "failed"}
    unready_response = _post(app_client, monkeypatch, unready)
    assert unready_response.status_code == 400
    unready_body = unready_response.get_json()
    assert set(unready_body) == {"error", "batch_id"}
    assert unready_body["error"] == "Scenario generation failed."


def _type_error_on_ddl(plan):
    def handler(sql, _params):
        text = " ".join(str(sql).split())
        if text.startswith(("CREATE SEQUENCE", "CREATE TABLE", "GRANT")):
            raise TypeError(LEAK)
        if "FROM ALL_USERS" in text:
            return [(1,)]
        if "FROM ALL_TABLES" in text or "FROM ALL_SEQUENCES" in text:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in text:
            return plan_catalog_rows(plan)
        return []

    return handler


def test_generate_type_error_after_ddl_uses_outcome_unknown_envelope(
    app_client, monkeypatch, caplog, captured_errors,
):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)
    monkeypatch.setattr(config, "ODI_EXEC_USER", "ODI_EXEC")
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan)
    services = make_services(orchestrator, ddl_handler=_type_error_on_ddl(plan))
    monkeypatch.setattr(scd2_service, "default_services", lambda: services)
    response = app_client.post("/api/scd2/generate", json={**REQUEST, "approved": True})
    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Internal server error.",
        "error_type": "TypeError",
        "status": "outcome_unknown",
        "outcome_unknown": True,
        "manual_review_required": True,
    }
    assert any(call[0] == "groovy" for call in orchestrator.calls)
    _assert_our_logs_hide_the_token(response, caplog)


def test_run_type_error_after_remote_call_uses_outcome_unknown_envelope(
    app_client, monkeypatch, caplog, captured_errors,
):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(
        plan, responses={"scd2_probe.groovy": probe_payload(plan, mapping_exists=True, scenario_exists=True)},
    )

    def catalog(sql, _params):
        if "FROM ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(plan)
        return [(0,)]

    calls = {"n": 0}

    def collect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise TypeError(LEAK)
        return {"status": "ok", "stock_contract_valid": True, "source_current_count_equal": True}

    monkeypatch.setattr(scd2_service.scd2_postflight, "collect_stock_postflight", collect)
    services = make_services(orchestrator, catalog_handler=catalog)
    monkeypatch.setattr(scd2_service, "default_services", lambda: services)
    import config
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)
    response = app_client.post("/api/scd2/run", json={**RUN_BODY, "approved": True})
    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Internal server error.",
        "error_type": "TypeError",
        "status": "outcome_unknown",
        "outcome_unknown": True,
        "manual_review_required": True,
    }
    assert any(call[0] == "scenario_run" for call in orchestrator.calls)
    _assert_our_logs_hide_the_token(response, caplog)


def test_unmarked_route_keeps_the_global_handler_body(app_client, monkeypatch, caplog, captured_errors):
    import routes.status as status

    def boom(*_args, **_kwargs):
        raise TypeError(LEAK)

    monkeypatch.setattr(status._PROBE_CACHE, "read", boom)
    response = app_client.get("/api/status")
    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal server error.", "error_type": "TypeError"}
    assert response.get_data() == b'{"error":"Internal server error.","error_type":"TypeError"}\n'
    _assert_our_logs_hide_the_token(response, caplog)


def test_acceptance_rejects_success_when_history_was_not_saved():
    import odi81_app_acceptance as driver
    import odi81_isolated_fixtures as isolated

    payload = {
        "status": "SUCCESS",
        "scenario": "MAP_ODI81N_F1_SCN",
        "version": driver.CLASSIC_VERSION,
        "context": "GLOBAL",
        "truncate_target_table": False,
        "state_persist_failed": True,
        "warning": COMPLETED_WARNING,
    }
    with pytest.raises(isolated._Blocked):
        driver._accept_classic_run("F1", "GLOBAL", 200, payload)
