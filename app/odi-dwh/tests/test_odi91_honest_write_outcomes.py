"""ODI-91/92: a write that was attempted must not come back as an empty failure.

Mocked and offline only. These tests lock the response bytes for exceptions that
happen before any write, and the explicit outcome once a write has been attempted.
"""

from __future__ import annotations

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
    assert body["state_persist_failed"] is True
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
    assert body["state_persist_failed"] is True
    assert body.get("error") != "mapping run failed."
    _assert_no_leak(response, caplog)


def test_persist_failure_is_reported_on_the_known_result(app_client, monkeypatch, caplog):
    class Broken(_quiet_state()):
        def upsert_state(self, *_args, **_kwargs):
            raise RuntimeError(LEAK)

    response = _post(app_client, monkeypatch, _pipeline(done()), state_manager=Broken)
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "SUCCESS"
    assert body["batch_id"].startswith("RUN-")
    assert body["remote"]["session_status"] == "DONE"
    assert body["state_persist_failed"] is True
    assert "error" not in body
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


def test_other_applied_created_changed_counters_require_review():
    for key in ("grants_applied", "objects_created", "rows_changed"):
        payload, status = scd2_service._generate_response(
            "blocked_target_incomplete", None, [{"step": "target_ddl", key: 1}], 409,
        )
        assert status == 409
        assert payload["known_side_effects"] is True
        assert payload["manual_review_required"] is True
        assert payload["outcome_unknown"] is False


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
