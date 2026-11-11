"""Service-level stock SQL and context gates on synthetic SQLite connections."""

from __future__ import annotations

import pytest
from dataclasses import replace

import execution_identity
import remote_run_contract
import scd2_builder
import scd2_service
from tests.scd2_support import REQUEST, SOURCE_COLUMNS, probe_payload
from tests.test_remote_run_contract import RemoteFakeOrchestrator
from tests.test_scd2_stock_postflight import Connection, Cursor


class StockCursor(Cursor):
    def execute(self, sql):
        translated = sql.replace('"DM"."DIM_CUSTOMER"', '"DWH"."DIM_TEST"')
        translated = translated.replace('"DWH"."ODS_CUSTOMERS"', '"DWH"."SRC_TEST"')
        translated = translated.replace('"CUST_ID"', '"NK"')
        translated = translated.replace("DATE '2300-01-01'", "'2300-01-01'")
        super().execute(translated)


class StockConnection(Connection):
    def cursor(self):
        return StockCursor(self)


def setup(monkeypatch, *, before=None, after=None):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(
        plan, target_datastore_exists=True, mapping_exists=True, scenario_exists=True
    )})
    monkeypatch.setattr(scd2_service, "_target_is_scd2", lambda *_: True)
    first = StockConnection(before or [], source_rows=1)
    second = StockConnection(after if after is not None else [(1, "1", "2026-01-01", "2400-01-01")], source_rows=1)
    pending = [first, second]
    opened = []
    source_opened = []

    def catalog():
        connection = pending.pop(0)
        opened.append(connection)
        return connection

    def source_ddl():
        connection = StockConnection([], source_rows=1)
        source_opened.append(connection)
        return connection

    source_ddl.opened = source_opened
    services = scd2_service.Scd2Services(catalog, source_ddl, "DWH", lambda: orchestrator)
    return orchestrator, services, opened, first, second


@pytest.fixture(autouse=True)
def grants(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)


def test_real_stock_sql_is_used_before_and_after_run(monkeypatch):
    orchestrator, services, opened, before, after = setup(monkeypatch)
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 200 and payload["status"] == "run_completed"
    assert payload["preflight"]["contract_profile"] == payload["postflight"]["contract_profile"] == "stock_1_0_2400"
    assert payload["postflight"]["stock_contract_valid"] is True
    assert payload["postflight"]["source_current_count_equal"] is True
    assert payload["postflight"]["business_parity_proven"] is False
    assert payload["scenario_execution_completed"] is True and payload["postflight_checks_passed"] is True
    assert len(opened) == 2
    assert len(services.ddl_connection.opened) == 2
    assert before.rollbacks == after.rollbacks == before.closes == after.closes == 1
    assert before.statements[0] == after.statements[0] == "SET TRANSACTION READ ONLY"
    for source in services.ddl_connection.opened:
        assert source.rollbacks == source.closes == 1
        assert source.statements == [
            "SET TRANSACTION READ ONLY", 'SELECT COUNT(*) FROM "DWH"."SRC_TEST"',
        ]
    assert any(call[0] == "scenario_run" for call in orchestrator.calls)
    identity_calls = [call for call in orchestrator.calls if call[:2] == ("groovy", "execution_identity_probe.groovy")]
    assert len(identity_calls) == 1 and identity_calls[0][3]["odi.execution.context"] == "GLOBAL"


@pytest.mark.parametrize("context", ["DEV", "PROD", "OTHER_CONTEXT"])
def test_unproven_context_is_rejected_before_any_external_callback(monkeypatch, context):
    _, services, opened, _, _ = setup(monkeypatch)
    services = replace(services, orchestrator_factory=lambda: pytest.fail("Unproven context reached repository"))
    payload, status = scd2_service.run({**REQUEST, "context": context}, services, approved=True)
    assert status == 400 and payload["status"] == "invalid_request" and opened == []


def test_fresh_topology_refusal_stops_before_counts_and_run(monkeypatch):
    orchestrator, services, opened, _, _ = setup(monkeypatch)
    orchestrator.prove_execution_identity = lambda **_: (
        execution_identity.public_blocked("execution_identity_ambiguous", connected=True), None
    )
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 409 and payload["status"] == "blocked_execution_identity"
    assert opened == [] and not any(call[0] == "scenario_run" for call in orchestrator.calls)


@pytest.mark.parametrize("field,value", [("model_code", "OTHER"), ("context_code", "DEV"), ("schema_name", "OTHER")])
def test_wrong_internal_identity_binding_is_not_a_run_authority(monkeypatch, field, value):
    orchestrator, services, opened, _, _ = setup(monkeypatch)
    identity = {"username": "SYNTHETIC_RUNNER", "model_code": "TGT_DM", "context_code": "GLOBAL", "schema_name": "DM"}
    identity[field] = value
    proof = execution_identity.ExecutionIdentityProof(**identity)
    orchestrator.prove_execution_identity = lambda **_: ({"status": "passed"}, proof)
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 409 and payload["status"] == "blocked_execution_identity" and opened == []
    assert "SYNTHETIC_RUNNER" not in str(payload)


def test_disabled_grants_do_not_probe_or_grant_on_run(monkeypatch):
    import config

    orchestrator, services, _, _, _ = setup(monkeypatch)
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    orchestrator.prove_execution_identity = lambda **_: pytest.fail("Disabled grants reached identity probe")
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 200 and payload["scenario"]["context"] == "GLOBAL"


def test_postflight_rejects_incomplete_source_pair_without_opening_connection(monkeypatch):
    _, services, opened, _, _ = setup(monkeypatch)
    payload, status = scd2_service.postflight({
        "owner": "DM", "table": "DIM_CUSTOMER", "natural_key": "CUST_ID", "source_owner": "DWH"
    }, services)
    assert status == 400 and payload["status"] == "invalid_request" and opened == []


def test_legacy_before_state_stops_the_native_run(monkeypatch):
    orchestrator, services, _, _, _ = setup(monkeypatch, before=[(1, "Y", "2026-01-01", "9999-12-31")])
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 503 and payload["status"] == "blocked_count_preflight"
    assert payload["preflight"]["status"] == "invalid_stock_contract"
    assert not any(call[0] == "scenario_run" for call in orchestrator.calls)


@pytest.mark.parametrize("after", [
    [(1, "Y", "2026-01-01", "9999-12-31")],
    [(1, "1", "2026-01-01", "9999-12-31")],
    [(1, "1", "2026-01-01", "2400-01-01")] * 2,
    [],
])
def test_native_done_with_invalid_or_incomplete_postflight_is_not_accepted(monkeypatch, after):
    _, services, _, _, _ = setup(monkeypatch, after=after)
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 502 and payload["status"] == "run_completed_postflight_failed"
    assert payload["scenario_execution_completed"] is True and payload["postflight_checks_passed"] is False
    assert payload["manual_review_required"] is True


@pytest.mark.parametrize("failure", ["fail_read", "fail_rollback", "fail_close"])
def test_postflight_failure_after_native_done_cannot_be_green(monkeypatch, failure):
    _, services, _, _, after = setup(monkeypatch)
    setattr(after, failure, True)
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 502 and payload["postflight_checks_passed"] is False
    assert payload["postflight"]["status"] == "error"
    assert "SYNTHETIC_PRIVATE" not in str(payload)


@pytest.mark.parametrize("invalid_postflight", [False, True])
def test_unknown_remote_completion_remains_unknown_and_requires_review(monkeypatch, invalid_postflight):
    after = [] if invalid_postflight else None
    orchestrator, services, _, _, _ = setup(monkeypatch, after=after)
    orchestrator.remote_result = remote_run_contract.transport_failure("run", "RemoteTimeout")
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 502 and payload["status"] == "run_unknown"
    assert payload["scenario_execution_completed"] is None and payload["manual_review_required"] is True


def test_failed_started_scenario_requires_manual_review_of_possible_partial_effects(monkeypatch):
    orchestrator, services, _, _, _ = setup(monkeypatch)
    orchestrator.remote_result = remote_run_contract.result(
        "run", status="failed", stage="run", error_code="ScenarioRunFailed", agent=True,
        repository=True, may_have_started=True, session_status="ERROR"
    )
    payload, status = scd2_service.run(REQUEST, services, approved=True)
    assert status == 502 and payload["status"] == "run_failed"
    assert payload["scenario_execution_completed"] is False and payload["manual_review_required"] is True


@pytest.mark.parametrize("context", [True, 1, None, [], {}])
def test_nonstring_context_never_reaches_external_callbacks(monkeypatch, context):
    _, services, opened, _, _ = setup(monkeypatch)
    services = replace(services, orchestrator_factory=lambda: pytest.fail("Invalid context reached callback"))
    assert scd2_service.run({**REQUEST, "context": context}, services, approved=True)[1] == 400
    assert opened == []
