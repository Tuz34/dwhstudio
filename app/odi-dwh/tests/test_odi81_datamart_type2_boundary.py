"""Synthetic route proof that DM_ targets cannot enter classic generation."""

import pytest

import scd2_builder
import scd2_service
from mapping_generator import MappingGenerator
from tests.scd2_support import REQUEST, SOURCE_COLUMNS, make_services, plan_catalog_rows
from tests.test_remote_run_contract import RemoteFakeOrchestrator


WARNING = "Datamart targets are supported only for Type 2 history."
CODE = "datamart_requires_type2"
CLASSIC_PARAMS = {
    "mapping_name": "MAP_HR_ORDERS_TO_DM",
    "src_model": "ORACLE_SOURCE",
    "src_table": "HR.ORDERS",
    "tgt_model": "DWH",
    "tgt_owner": "DWH",
    "tgt_table": "DM_HR_ORDERS",
    "integration_type": "CONTROL_APPEND",
}


@pytest.mark.parametrize("target", ["DM_X", " dm_x ", "DWH.DM_X"])
def test_classic_validator_rejects_dm_prefix_with_fixed_code(target):
    with pytest.raises(ValueError) as caught:
        MappingGenerator().validate_params({**CLASSIC_PARAMS, "tgt_table": target})
    assert str(caught.value) == WARNING
    assert caught.value.error_code == CODE


def _forbid_external_steps(monkeypatch):
    calls = []

    def forbid(*_args, **_kwargs):
        calls.append("external step")
        raise AssertionError("repository, DB, or ODI step reached")

    for name in (
        "_decide_effective_audit", "_preflight_odi_repository",
        "_resolve_odi_repository_bindings", "_build_target_column_plan",
        "_create_target_table", "_create_odi_mapping",
    ):
        monkeypatch.setattr(MappingGenerator, name, forbid)
    monkeypatch.setattr("db.get_connection", forbid)
    monkeypatch.setattr("odi_orchestrator.OdiOrchestrator", forbid)
    return calls


def test_generate_route_rejects_dm_prefix_before_any_external_step(app_client, monkeypatch):
    calls = _forbid_external_steps(monkeypatch)
    response = app_client.post(
        "/api/mapping/generate",
        json={"engine": "classic", "approved": True, "params": CLASSIC_PARAMS},
    )
    assert response.status_code == 200  # Existing classic result envelope.
    assert response.get_json()["overall_status"] == "FAILED"
    assert response.get_json()["error_code"] == CODE
    assert response.get_json()["error"] == WARNING
    assert response.get_json()["steps"] == {}
    assert calls == []


def test_target_preflight_route_rejects_dm_prefix_before_any_external_step(app_client, monkeypatch):
    calls = _forbid_external_steps(monkeypatch)
    response = app_client.post(
        "/api/mapping/target-preflight", json={"params": CLASSIC_PARAMS},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": WARNING, "error_code": CODE}
    assert calls == []


def test_type2_plan_and_generate_still_accept_dm_prefix(app_client, monkeypatch):
    request = {**REQUEST, "target_owner": "DWH", "target_table": "DM_DIM_CUSTOMER"}
    plan = scd2_builder.build_scd2_plan(request, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan)

    def ddl(sql, _params):
        if "FROM ALL_USERS" in sql:
            return [(1,)]
        if "FROM ALL_TABLES" in sql or "FROM ALL_SEQUENCES" in sql:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(plan)
        return []

    services = make_services(orchestrator, ddl_handler=ddl)
    monkeypatch.setattr(scd2_service, "default_services", lambda: services)
    preview = app_client.post(
        "/api/scd2/plan", json={**request, "source_columns": SOURCE_COLUMNS},
    )
    assert preview.status_code == 200
    assert preview.get_json()["plan"]["target"] == {"owner": "DWH", "table": "DM_DIM_CUSTOMER"}
    generated = app_client.post("/api/scd2/generate", json={**request, "approved": True})
    assert generated.status_code == 200
    assert generated.get_json()["status"] == "generated"
