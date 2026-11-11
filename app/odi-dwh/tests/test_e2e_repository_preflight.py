"""Feynman regressions: repository availability and physical source identity, offline only."""

from unittest.mock import Mock

import json

import pytest

import mapping_generator as mg
import odi_orchestrator as oo


BASE = dict(mapping_name="MAP_TEST", src_model="ORACLE_HR", src_table="HR.TEST",
            tgt_model="ORACLE_ODS", tgt_owner="DWH", tgt_table="ODS_TEST",
            update_key_columns=["ID"], ikm_name="IKM Oracle Merge")
SOURCE = dict(model_code="AUDIT_MODEL", datastore_name="AUDIT_ALIAS", resource_name="ODS_TEST",
              physical_schema="AUDIT_USER", context="GLOBAL", datastore_exists=True, datastore_id="42")


def proof(**extra):
    return dict(tool="odi.mapping_preflight", status="passed", side_effects=False,
                repository_connection_attempted=True, ikm_name="IKM Oracle Merge", ikm_id="7",
                ikm_match_count=1, source=None, **extra)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(mg, "SOURCE_ORACLE_SCHEMAS", ["HR", "SH"])


def pipeline(monkeypatch, result):
    generator = mg.MappingGenerator()
    events = []

    def preflight(self, **kwargs):
        events.append("repository_preflight")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(oo.OdiOrchestrator, "preflight_mapping", preflight, raising=False)
    for method in ("_create_target_table", "_resolve_odi_repository_bindings", "_register_odi_datastores",
                   "_create_odi_mapping", "_populate_lineage", "_generate_tests", "_take_snapshot",
                   "_log_to_mapping_log"):
        monkeypatch.setattr(generator, method, lambda *a, _method=method, **kw: events.append(_method) or {})
    return generator, events


@pytest.mark.parametrize("response", [
    {**proof(), "status": "blocked", "ikm_match_count": 0},
    {**proof(), "status": "blocked", "ikm_match_count": 2},
    {**proof(), "status": "error"}, RuntimeError("synthetic lookup failure"),
])
def test_repository_lookup_failure_precedes_every_persistent_step(response, monkeypatch):
    generator, events = pipeline(monkeypatch, response)
    result = generator.generate_from_params(dict(BASE, drop_target_if_exists=True, replace_existing_mapping=True))
    assert result["overall_status"] == "FAILED"
    assert events == ["repository_preflight"]
    assert "create_target_table" not in result["steps"]


def test_exact_repository_success_precedes_ddl_and_registration(monkeypatch):
    generator, events = pipeline(monkeypatch, proof())
    result = generator.generate_from_params(BASE)
    assert result["overall_status"] == "SUCCESS"
    assert events[:3] == ["repository_preflight", "_resolve_odi_repository_bindings", "_create_target_table"]
    assert result["params"]["_odi_ikm_id"] == "7"


def test_explicit_internal_owner_is_not_replaced_by_model_hint():
    generator = mg.MappingGenerator()
    params = generator.validate_params(dict(BASE, src_model="ORACLE_ODS", src_table="AUDIT_USER.ODS_TEST"))
    assert params["src_owner"] == "AUDIT_USER"
    assert generator._source_schema_hint(params) == "AUDIT_USER"


def test_binding_uses_verified_source_not_preferred_duplicate_model(monkeypatch):
    generator = mg.MappingGenerator()
    params = generator.validate_params(dict(BASE, src_model="ORACLE_ODS", src_table="AUDIT_USER.ODS_TEST"))
    params["_odi_source_identity"] = dict(SOURCE)
    reader = Mock()
    reader.resolve_datastore.return_value = {"model_code": "ORACLE_ODS", "datastore_name": "ODS_TEST"}
    monkeypatch.setattr(generator, "_get_odi_repo_reader", lambda: reader)
    monkeypatch.setattr(generator, "_find_repository_mapping", lambda _: None)
    result = generator._resolve_odi_repository_bindings(params)
    assert params["_odi_src_model_code"] == "AUDIT_MODEL"
    assert params["_odi_src_datastore_name"] == "AUDIT_ALIAS"
    assert result["source"]["physical_schema"] == "AUDIT_USER"
    assert not any(call.kwargs.get("role") == "source" for call in reader.resolve_datastore.call_args_list)


def test_internal_binding_without_physical_proof_is_blocked(monkeypatch):
    generator = mg.MappingGenerator()
    params = generator.validate_params(dict(BASE, src_model="ORACLE_ODS", src_table="AUDIT_USER.ODS_TEST"))
    reader = Mock()
    monkeypatch.setattr(generator, "_get_odi_repo_reader", lambda: reader)
    with pytest.raises(RuntimeError):
        generator._resolve_odi_repository_bindings(params)
    reader.resolve_datastore.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"ikm_match_count": 0}, {"ikm_match_count": 2}, {"ikm_match_count": True},
    {"ikm_id": None}, {"ikm_name": "IKM Oracle Insert"}, {"side_effects": True},
    {"repository_connection_attempted": False}, {"tool": "other"},
])
def test_malformed_success_cannot_authorize_persistent_steps(changes, monkeypatch):
    generator, events = pipeline(monkeypatch, {**proof(), **changes})
    result = generator.generate_from_params(BASE)
    assert result["overall_status"] == "FAILED"
    assert events == ["repository_preflight"]


@pytest.mark.parametrize("changes", [
    {"physical_schema": "DWH"}, {"resource_name": "OTHER"}, {"context": "OTHER"},
    {"datastore_exists": "true"}, {"datastore_id": None},
])
def test_internal_identity_mismatch_fails_before_ddl(changes, monkeypatch):
    generator, events = pipeline(monkeypatch, {**proof(), "source": {**SOURCE, **changes}})
    result = generator.generate_from_params(dict(BASE, src_model="ORACLE_ODS", src_table="AUDIT_USER.ODS_TEST"))
    assert result["overall_status"] == "FAILED"
    assert events == ["repository_preflight"]


def test_preflight_wrapper_is_approval_gated_metadata_only(monkeypatch):
    run = Mock(return_value=json.dumps({**proof(), "untrusted_extra": ["not_returned"]}))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    orchestrator = oo.OdiOrchestrator()
    assert orchestrator.preflight_mapping(ikm_name=BASE["ikm_name"])["status"] == "blocked"
    run.assert_not_called()
    result = orchestrator.preflight_mapping(ikm_name=BASE["ikm_name"], approved=True)
    assert result == proof()
    assert run.call_args.args == ("mapping_preflight.groovy", {"odi.ikm_name": BASE["ikm_name"], "odi.context": "GLOBAL"})


@pytest.mark.parametrize("output", ["not JSON", "[]", '{"status":"passed"}', '{"status":"blocked","code":[]}'])
def test_preflight_wrapper_rejects_unproven_outputs(output, monkeypatch):
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", lambda *a: output)
    result = oo.OdiOrchestrator().preflight_mapping(ikm_name=BASE["ikm_name"], approved=True)
    assert result["status"] == "blocked" and result["side_effects"] is False


def test_verified_source_identity_is_transported_to_actual_binding(monkeypatch):
    generator = mg.MappingGenerator()
    params = generator.validate_params(dict(BASE, src_model="ORACLE_ODS", src_table="AUDIT_USER.ODS_TEST"))
    params.update(_odi_source_identity=dict(SOURCE), _odi_ikm_id="7", _odi_repository={"mapping_exists": False},
                  _odi_src_model_code="AUDIT_MODEL", _odi_src_datastore_name="AUDIT_ALIAS",
                  _odi_tgt_model_code="TARGET", _odi_tgt_datastore_name="ODS_TEST")
    run = Mock(return_value="SUCCESS")
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    generator._create_odi_mapping(params)
    sent = run.call_args.args[1]
    assert sent["odi.src_model"] == "AUDIT_MODEL" and sent["odi.src_table"] == "AUDIT_ALIAS"
    assert sent["odi.required_src_owner"] == "AUDIT_USER"
    assert sent["odi.required_src_resource"] == "ODS_TEST"
    assert sent["odi.required_src_id"] == "42" and sent["odi.source_context"] == "GLOBAL"
    assert sent["odi.required_ikm_id"] == "7"


@pytest.mark.parametrize("output", [
    json.dumps({"tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False, "code": "ikm_not_imported"}),
    json.dumps({"tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False, "code": "ikm_ambiguous"}),
    "invalid response",
])
def test_real_wrapper_blocks_target_connection_drop_grants_and_audit(output, monkeypatch):
    import target_table_service as ts

    generator = mg.MappingGenerator()
    run = Mock(return_value=output)
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    guards = []
    for module, name in [(ts, "_connection"), (ts, "_drop_target"), (ts, "_apply_grants"),
                         (ts, "_write_creation_audit"), (generator, "_get_odi_repo_reader"),
                         (generator, "_register_odi_datastores"), (generator, "_create_odi_mapping"),
                         (generator, "_log_to_mapping_log")]:
        guard = Mock(side_effect=AssertionError("persistent or downstream operation reached"))
        monkeypatch.setattr(module, name, guard)
        guards.append(guard)
    result = generator.generate_from_params(dict(BASE, drop_target_if_exists=True, replace_existing_mapping=True))
    assert result["error_code"] in {"ikm_not_imported", "repository_preflight_failed"}
    assert run.call_args.args[0] == "mapping_preflight.groovy"
    assert run.call_count == 1
    for guard in guards:
        guard.assert_not_called()


def test_wrapper_proves_explicit_owner_and_context_before_binding(monkeypatch):
    run = Mock(return_value=json.dumps({**proof(), "source": SOURCE}))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    generator = mg.MappingGenerator()
    params = generator.validate_params(dict(BASE, src_model="ORACLE_ODS", src_table="AUDIT_USER.ODS_TEST"))
    generator._preflight_odi_repository(params)
    assert params["_odi_source_identity"] == SOURCE
    assert run.call_args.args[1] == {"odi.ikm_name": BASE["ikm_name"], "odi.src_owner": "AUDIT_USER",
                                     "odi.src_table": "ODS_TEST", "odi.context": "GLOBAL"}


def _quiet_database(monkeypatch):
    import db

    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(db, "get_connection", lambda use_ddl=True: Connection())


_PREFLIGHT_COLUMNS = (
    {"COLUMN_NAME": "ENTITY_ID", "ORACLE_TYPE": "NUMBER(10)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "ATTR_TEXT", "ORACLE_TYPE": "VARCHAR2(40)", "IS_NULLABLE": "Y"},
)


def _f1_generator():
    import odi81_app_acceptance as driver

    generator = mg.MappingGenerator()
    generator._source_columns_for = lambda _params: [dict(column) for column in _PREFLIGHT_COLUMNS]
    return generator, driver.classic_params("F1")


def _f1_identity() -> dict:
    return {
        "model_code": "ORACLE_DWH",
        "datastore_name": "ODI81N_SRC_F1",
        "resource_name": "ODI81N_SRC_F1",
        "physical_schema": "DWH",
        "context": "GLOBAL",
        "datastore_exists": True,
        "datastore_id": "42",
    }


def test_internal_preflight_reports_repository_datastores(monkeypatch):
    """4936390 resolved bindings before the read-only identity existed."""
    _quiet_database(monkeypatch)
    monkeypatch.setattr(
        "config.ODI_SOURCE_MODEL_BINDINGS", '{"GLOBAL":{"DWH":"ORACLE_DWH"}}', raising=False,
    )
    seen = []

    def preflight(self, **kwargs):
        seen.append(dict(kwargs))
        payload = proof()
        payload["ikm_name"] = kwargs["ikm_name"]
        payload["source"] = _f1_identity()
        return payload

    monkeypatch.setattr(oo.OdiOrchestrator, "preflight_mapping", preflight)
    generator, params = _f1_generator()
    reader = Mock()
    target = {
        "model_code": "ORACLE_ODS", "datastore_name": "ODS_ODI81N_F1",
        "resource_name": "ODS_ODI81N_F1", "physical_schema": "DWH", "context": "GLOBAL",
        "datastore_exists": True, "datastore_id": "9",
    }
    reader.resolve_datastore.return_value = target
    reader.resolve_model.return_value = target
    reader.mapping_exists.return_value = {"mapping_name": "MAP_ODI81N_F1"}
    monkeypatch.setattr(generator, "_get_odi_repo_reader", lambda: reader)
    result = generator.target_table_preflight(params)
    assert seen and seen[0]["src_owner"] == "DWH" and seen[0]["src_table"] == "ODI81N_SRC_F1"
    repository = result["repository"]
    assert repository["status"] == "passed"
    assert repository["mapping_exists"] is True
    assert repository["source_datastore_exists"] is True
    assert repository["target_datastore_exists"] is True
    assert "error" not in repository
    assert not any(call.kwargs.get("role") == "source" for call in reader.resolve_datastore.call_args_list)


def test_repository_preflight_failure_is_unavailable_without_exception_text(app_client, monkeypatch):
    import odi81_app_acceptance as driver

    _quiet_database(monkeypatch)

    def explode(self, _params):
        raise RuntimeError("password=secret host=10.1.2.3")

    monkeypatch.setattr(
        mg.MappingGenerator, "_source_columns_for",
        lambda self, _params: [dict(column) for column in _PREFLIGHT_COLUMNS],
    )
    monkeypatch.setattr(mg.MappingGenerator, "_preflight_odi_repository", explode)
    response = app_client.post("/api/mapping/target-preflight", json=driver.classic_params("F1"))
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "OK"
    assert body["repository"]["status"] == "unavailable"
    assert body["repository"]["error"] == "repository_preflight_failed"
    assert body["repository"]["mapping_exists"] is None
    text = response.get_data(as_text=True)
    assert "secret" not in text and "10.1.2.3" not in text and "RuntimeError" not in text


def test_external_preflight_keeps_the_reader_path(monkeypatch):
    _quiet_database(monkeypatch)
    called = []
    monkeypatch.setattr(
        mg.MappingGenerator, "_preflight_odi_repository", lambda self, params: called.append(params),
    )
    generator, params = _f1_generator()
    params = dict(params, src_model="ORACLE_SOURCE")
    reader = Mock()
    found = {"model_code": "ORACLE_SOURCE", "datastore_name": "ODI81N_SRC_F1", "datastore_exists": True}
    reader.resolve_datastore.return_value = found
    reader.resolve_model.return_value = found
    reader.mapping_exists.return_value = None
    monkeypatch.setattr(generator, "_get_odi_repo_reader", lambda: reader)
    result = generator.target_table_preflight(params)
    assert called == []
    assert result["repository"]["status"] == "passed"
    assert result["repository"]["mapping_exists"] is False
    assert result["repository"]["source_datastore_exists"] is True
    assert "error" not in result["repository"]
    reader.resolve_datastore.side_effect = RuntimeError("password=secret")
    failed = generator.target_table_preflight(params)
    assert failed["repository"]["status"] == "unavailable"
    assert failed["repository"]["error"] == "repository_preflight_failed"
    assert "secret" not in json.dumps(failed)
