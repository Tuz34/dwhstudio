"""Synthetic route proof that DM_ targets cannot enter classic generation."""

import pytest

import app as app_module
import scd2_builder
import scd2_service
from mapping_graph import node_validate
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

QUALIFIED_DM_SPELLINGS = [
    "DWH. DM_X", "DWH.  DM_X", "DWH.\tDM_X", "DWH.\u00a0DM_X",
    "dwh. dM_x", " \tDWH. DM_X",
]


@pytest.mark.parametrize("target", ["DM_X", " dm_x ", "DWH.DM_X", *QUALIFIED_DM_SPELLINGS])
def test_classic_validator_rejects_dm_prefix_with_fixed_code(target):
    with pytest.raises(ValueError) as caught:
        MappingGenerator().validate_params({**CLASSIC_PARAMS, "tgt_table": target})
    assert str(caught.value) == WARNING
    assert caught.value.error_code == CODE


@pytest.mark.parametrize("raw, normalized", [
    ("DM_", "DM_"), ("DWH. DM_X", "DM_X"), ("DWH.  DM_X", "DM_X"),
    ("DWH.\tDM_X", "DM_X"), ("DWH.\u00a0DM_X", "DM_X"),
    ("dwh. dM_x", "DM_X"), (" \tDWH. DM_X", "DM_X"),
    ("ODS_DM_X", "ODS_DM_X"), ("XDM_Y", "XDM_Y"), ("DWH.ODS_X", "ODS_X"),
])
def test_datamart_rule_tracks_the_normalized_target_identifier(raw, normalized):
    generator = MappingGenerator()
    # Check the actual identifier validation result used by the generator.
    assert generator._validate_identifier(raw.strip().upper().split(".")[-1], "tgt_table") == normalized
    params = {**CLASSIC_PARAMS, "tgt_table": raw}
    if normalized.startswith("DM_"):
        with pytest.raises(ValueError) as caught:
            generator.validate_params(params)
        assert str(caught.value) == WARNING
        assert caught.value.error_code == CODE
    else:
        assert generator.validate_params(params)["tgt_table"] == normalized


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


@pytest.mark.parametrize("target", ["DM_HR_ORDERS", *QUALIFIED_DM_SPELLINGS])
def test_generate_route_rejects_dm_prefix_before_any_external_step(app_client, monkeypatch, target):
    calls = _forbid_external_steps(monkeypatch)
    response = app_client.post(
        "/api/mapping/generate",
        json={"engine": "classic", "approved": True,
              "params": {**CLASSIC_PARAMS, "tgt_table": target}},
    )
    assert response.status_code == 200  # Existing classic result envelope.
    assert response.get_json()["overall_status"] == "FAILED"
    assert response.get_json()["error_code"] == CODE
    assert response.get_json()["error"] == WARNING
    assert response.get_json()["steps"] == {}
    assert calls == []


@pytest.mark.parametrize("target", ["DM_HR_ORDERS", *QUALIFIED_DM_SPELLINGS])
def test_target_preflight_route_rejects_dm_prefix_before_any_external_step(app_client, monkeypatch, target):
    calls = _forbid_external_steps(monkeypatch)
    response = app_client.post(
        "/api/mapping/target-preflight",
        json={"params": {**CLASSIC_PARAMS, "tgt_table": target}},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": WARNING, "error_code": CODE}
    assert calls == []


def test_graph_validation_preserves_datamart_code_through_default_route(app_client, monkeypatch):
    calls = _forbid_external_steps(monkeypatch)
    failed = node_validate({"raw_params": {**CLASSIC_PARAMS, "tgt_table": "DWH. DM_X"}, "log": []})
    assert failed["overall_status"] == "failed"
    assert failed["error_code"] == CODE
    monkeypatch.setattr(app_module, "run_mapping_graph", lambda **_kwargs: failed)
    response = app_client.post(
        "/api/mapping/generate",
        json={"approved": True, "params": {**CLASSIC_PARAMS, "tgt_table": "DWH. DM_X"}},
    )
    assert response.status_code == 200
    assert response.get_json()["error_code"] == CODE
    assert response.get_json()["error"] == WARNING
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
