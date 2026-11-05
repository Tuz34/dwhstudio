"""ODI-87 generic Run entry gate: mocked boundaries only, no DB, ODI or app bootstrap.

The guarantee under test is limited to ``execute_mapping_run``. Low-level runners
are out of scope and are not protected by this gate.
"""

from __future__ import annotations

import sys
import types
from itertools import product
from unittest.mock import Mock

import pytest
from flask import Flask

import mapping_run_service as service
import odi_orchestrator as oo
import remote_run_contract as contract
from tests.test_remote_run_contract import isolated_http_handler

FIXED = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
BLOCKED_PAYLOAD = {
    "status": "blocked_fixed_mapping_cutover_pending",
    "error": (
        "The fixed DIM_PRODUCT mapping cannot run through the generic mapping run path "
        "until a separately reviewed cutover proof replaces this gate."
    ),
}
# str.upper() maps the dotless i and the long s onto ASCII, so these reach the same mapping.
SPELLINGS = [
    FIXED,
    FIXED.lower(),
    "Map_Ods_Products_To_Dim_Product",
    f"  {FIXED}\t\n",
    f" {FIXED.lower()} ",
    "map_ods_products_to_dım_product",
    "map_odſ_products_to_dim_product",
]
VERSIONS = ["001", "002", "007", "LATEST", "9.9"]
CONTEXTS = ["GLOBAL", "DEVELOPMENT", "TEST"]
TRUNCATES = [True, False]
MATRIX = list(product(VERSIONS, CONTEXTS, TRUNCATES))


class BombApp:
    """Any use of the app module means work continued past the gate."""

    def __getattr__(self, name):
        raise AssertionError(f"app module reached: {name}")


def _bomb_module(name):
    module = types.ModuleType(name)

    def __getattr__(attr):
        raise AssertionError(f"{name} imported past the gate: {attr}")

    module.__getattr__ = __getattr__
    return module


@pytest.fixture
def downstream_bombs(monkeypatch):
    """Fail on import or construction of the orchestrator and on every later step."""
    monkeypatch.setitem(sys.modules, "odi_orchestrator", _bomb_module("odi_orchestrator"))
    monkeypatch.setattr(service.uuid, "uuid4", Mock(side_effect=AssertionError("batch id created")))
    monkeypatch.setattr(service.remote_run_contract, "parse", Mock(side_effect=AssertionError("health parsed")))
    for name in ("_persist_run_state", "_create_failure_alert", "_failure_outcome", "_unsupported_option_outcome"):
        monkeypatch.setattr(service, name, Mock(side_effect=AssertionError(f"{name} reached")))


def _options(name, version="001", context="GLOBAL", truncate=False):
    return service.MappingRunOptions(
        mapping_name=name, version=version, context=context, truncate_target_table=truncate,
    )


@pytest.mark.parametrize("version,context,truncate", MATRIX)
def test_every_option_combination_is_refused_with_zero_downstream_calls(downstream_bombs, version, context, truncate):
    outcome = service.execute_mapping_run(BombApp(), _options(FIXED, version, context, truncate))
    assert outcome.status_code == 409
    assert outcome.payload == BLOCKED_PAYLOAD


@pytest.mark.parametrize("name", SPELLINGS)
def test_name_normalization_matches_the_api_at_the_function_entry(downstream_bombs, name):
    outcome = service.execute_mapping_run(BombApp(), _options(name))
    assert outcome.status_code == 409
    assert outcome.payload == BLOCKED_PAYLOAD


def _route_handler():
    # Loaded before the bombs because the helper needs the real approval context.
    return isolated_http_handler("mapping_write.py", "api_mapping_run", {})


@pytest.mark.parametrize("name", SPELLINGS)
@pytest.mark.parametrize("version,context,truncate", [MATRIX[0], MATRIX[-1], ("007", "DEVELOPMENT", True)])
def test_actual_route_handler_returns_409_without_downstream_calls(monkeypatch, name, version, context, truncate):
    handler = _route_handler()
    monkeypatch.setitem(sys.modules, "odi_orchestrator", _bomb_module("odi_orchestrator"))
    monkeypatch.setattr(service.uuid, "uuid4", Mock(side_effect=AssertionError("batch id created")))
    body = {"approved": True, "mapping_name": name, "scenario_version": version,
            "context": context, "truncate_target_table": truncate}
    with Flask(__name__).test_request_context(json=body):
        response, status = handler()
    assert status == 409
    assert response.get_json() == BLOCKED_PAYLOAD


@pytest.mark.parametrize("extra", [
    {"ready": True}, {"cutover_ready": True}, {"cutover_proven": True}, {"force": True},
    {"override": True}, {"migration_complete": True}, {"skip_gate": "true"},
])
def test_client_supplied_readiness_is_not_an_escape_hatch(monkeypatch, extra):
    handler = _route_handler()
    monkeypatch.setitem(sys.modules, "odi_orchestrator", _bomb_module("odi_orchestrator"))
    body = {"approved": True, "mapping_name": FIXED, "truncate_target_table": False, **extra}
    with Flask(__name__).test_request_context(json=body):
        response, status = handler()
    assert status == 409
    assert response.get_json() == BLOCKED_PAYLOAD


@pytest.mark.parametrize("variable", [
    "ODI87_ALLOW_FIXED_MAPPING_RUN", "ODI87_CUTOVER_PROVEN", "DIM_PRODUCT_CUTOVER_READY", "ALLOW_FIXED_MAPPING_RUN",
])
def test_environment_is_not_an_escape_hatch(downstream_bombs, monkeypatch, variable):
    monkeypatch.setenv(variable, "true")
    outcome = service.execute_mapping_run(BombApp(), _options(FIXED))
    assert outcome.status_code == 409
    assert outcome.payload == BLOCKED_PAYLOAD


def test_refusal_echoes_no_request_value(downstream_bombs):
    name, version, context = " map_ods_products_to_dim_product ", "V9_MARKER", "CONTEXT_MARKER"
    outcome = service.execute_mapping_run(BombApp(), _options(name, version, context, True))
    rendered = repr(outcome.payload)
    for value in (name, name.strip(), FIXED, version, context, "True"):
        assert value not in rendered
    assert set(outcome.payload) == {"status", "error"}


def _unhealthy():
    return contract.result("health", error_code="AgentUnavailable")


@pytest.mark.parametrize("name", [
    "MAP_HR_EMPLOYEES_TO_ODS", f"{FIXED}_V2", f"X_{FIXED}", "MAP_ODS_PRODUCTS_TO_DIM_PRODUCTS", "DIM_PRODUCT",
])
def test_other_mappings_still_follow_the_original_health_gate(monkeypatch, name):
    orchestrator = Mock()
    orchestrator.remote_agent_health.return_value = _unhealthy()
    monkeypatch.setattr(oo, "OdiOrchestrator", lambda: orchestrator)
    with oo.groovy_approval_context(True):
        outcome = service.execute_mapping_run(Mock(), _options(name))
    assert outcome.status_code == 503
    assert outcome.payload == {"status": "blocked", "remote": _unhealthy()}
    orchestrator.remote_agent_health.assert_called_once_with(approved=True)
    orchestrator.update_mapping.assert_not_called()
    orchestrator.generate_scenario_metadata.assert_not_called()
    orchestrator.run_scenario_remote.assert_not_called()


def test_other_mapping_full_path_is_unchanged(monkeypatch):
    name, version = "MAP_HR_EMPLOYEES_TO_ODS", "003"
    scenario = f"{name}_SCN"
    done = contract.result("run", status="passed", stage="completed", agent=True,
                           repository=True, may_have_started=True, session_status="DONE")
    orchestrator = Mock()
    orchestrator.remote_agent_health.return_value = contract.result(
        "health", status="passed", stage="completed", agent=True)
    orchestrator.update_mapping.return_value = "unused"
    orchestrator.generate_scenario_metadata.return_value = "unused"
    orchestrator.run_scenario_remote.return_value = done
    monkeypatch.setattr(oo, "OdiOrchestrator", lambda: orchestrator)
    monkeypatch.setattr(service, "_options_updated", lambda output: True)
    monkeypatch.setattr(service, "_scenario_ready", lambda value, n, v: True)
    persist = Mock()
    monkeypatch.setattr(service, "_persist_run_state", persist)
    with oo.groovy_approval_context(True):
        outcome = service.execute_mapping_run(Mock(), _options(name, version, "DEVELOPMENT", True))
    assert outcome.status_code == 200
    batch_id = outcome.payload.pop("batch_id")
    assert batch_id.startswith("RUN-")
    assert outcome.payload == {"status": "SUCCESS", "scenario": scenario, "version": version,
                               "context": "DEVELOPMENT", "truncate_target_table": True, "remote": done}
    orchestrator.update_mapping.assert_called_once_with(
        mapping_name=name, operation="update_km", truncate_target_table=True)
    orchestrator.generate_scenario_metadata.assert_called_once_with(
        approved=True, scenario_name=scenario, mapping_name=name, version=version)
    orchestrator.run_scenario_remote.assert_called_once_with(
        approved=True, mapping_name=name, scenario_name=scenario, version=version, context="DEVELOPMENT")
    persist.assert_called_once()


def test_route_keeps_approval_before_the_gate(monkeypatch):
    handler = _route_handler()
    monkeypatch.setitem(sys.modules, "odi_orchestrator", _bomb_module("odi_orchestrator"))
    body = {"approved": False, "mapping_name": FIXED, "truncate_target_table": False}
    with Flask(__name__).test_request_context(json=body):
        response, status = handler()
    assert status == 400
    assert "approved:true" in response.get_json()["error"]
