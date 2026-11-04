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
    json.dumps({"tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False, "code": "ikm_missing"}),
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
    assert result["error_code"] == "repository_preflight_failed"
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
