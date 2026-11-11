"""Offline regression tests for persistent DWH binding and Type 2 source registration."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

import config
import mapping_generator
import odi81_isolated_fixtures as isolated
import odi81_scd2_fixtures
import odi_orchestrator
import scd2_builder
import scd2_service
import source_model_policy
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    make_services,
    probe_payload,
    source_catalog_rows,
)
from tests.test_remote_run_contract import RemoteFakeOrchestrator
from tests.test_repository_write_outcomes import failure as write_failure
from tests.test_scd2_service import ddl_handler


def _source_success(column_count: int) -> str:
    return "INFO: synthetic progress\n" + json.dumps({
        "tool": "odi.register_datastore", "status": "passed", "mode": "absent_only",
        "side_effects": True, "repository_connection_attempted": True,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
        "result": {"column_count": column_count, "commit_completed": True},
    })


def _source_failure(outcome: str) -> str:
    payload = write_failure(outcome=outcome)
    payload.update(tool="odi.register_datastore", mode="absent_only")
    return "INFO: synthetic progress\n" + json.dumps(payload)


class SourceOrchestrator(RemoteFakeOrchestrator):
    def __init__(self, plan, source_output=None, **kwargs):
        kwargs.setdefault("responses", {"scd2_probe.groovy": probe_payload(source_datastore_exists=False)})
        super().__init__(plan, **kwargs)
        self.source_output = source_output or _source_success(len(SOURCE_COLUMNS))

    def register_datastore(self, **kwargs):
        self.calls.append(("source_register", kwargs))
        return self.source_output


def _own_services(orchestrator, plan, *, ddl_log=None, table_exists=True):
    target_handler = ddl_handler(plan)

    def handler(sql, params):
        if sql == "SET TRANSACTION READ ONLY":
            return []
        if "FROM ALL_TAB_COLUMNS" in sql and params.get("table_name") == REQUEST["source_table"]:
            return source_catalog_rows()
        if "FROM ALL_TABLES" in sql and params.get("name") == REQUEST["source_table"]:
            return [(int(table_exists),)]
        return target_handler(sql, params)

    services = make_services(orchestrator, ddl_log=ddl_log, ddl_handler=handler)
    return replace(services, ddl_user=REQUEST["source_owner"])


def _plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


@pytest.mark.parametrize("raw", [None, "", "  \t"])
def test_unset_or_blank_config_binds_only_global_dwh(raw):
    effective = config.effective_source_model_bindings(raw)
    assert source_model_policy.parse_source_model_bindings(effective) == {
        "GLOBAL": {"DWH": scd2_builder.SOURCE_MODEL_BY_OWNER["DWH"]}
    }
    assert source_model_policy.trusted_source_model_code("GLOBAL", "DWH", effective) == "TGT_DWH"
    for owner, context in (("DM", "GLOBAL"), ("HR", "GLOBAL"), ("DWH", "DEV")):
        assert source_model_policy.trusted_source_model_code(context, owner, effective) is None
    odi81_scd2_fixtures.prove_owners(("DWH",), "GLOBAL", effective)
    with pytest.raises(isolated._Blocked, match="SourceModelUnbound"):
        odi81_scd2_fixtures.prove_owners(("DWH", "DM"), "GLOBAL", effective)


def test_explicit_binding_replaces_default_verbatim_and_invalid_fails_closed():
    explicit = ' {"GLOBAL":{"DM":"TGT_DM"}} '
    assert config.effective_source_model_bindings(explicit) == explicit
    assert source_model_policy.trusted_source_model_code("GLOBAL", "DWH", explicit) is None
    assert source_model_policy.trusted_source_model_code("GLOBAL", "DM", explicit) == "TGT_DM"
    with pytest.raises(isolated._Blocked, match="SourceModelUnbound"):
        odi81_scd2_fixtures.prove_owners(("DWH",), "GLOBAL", explicit)
    invalid = config.effective_source_model_bindings("{")
    assert invalid == "{"
    with pytest.raises(source_model_policy.SourceModelConfigError, match="source_model_config_invalid"):
        source_model_policy.trusted_source_model_code("GLOBAL", "DWH", invalid)
    with pytest.raises(isolated._Blocked, match="source_model_config_invalid"):
        odi81_scd2_fixtures.prove_owners(("DWH",), "GLOBAL", invalid)


def test_mapping_and_orchestrator_use_same_effective_default(monkeypatch):
    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", config.effective_source_model_bindings(None))
    from tests.test_odi88_source_model_binding import IKM, PARAMS, SOURCE, _proof

    source = {**SOURCE, "model_code": "TGT_DWH", "physical_schema": "DWH"}
    run = Mock(return_value=json.dumps(_proof(source=source)))
    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "run_groovy", run)
    params = mapping_generator.MappingGenerator().validate_params({
        **PARAMS, "src_model": "ORACLE_DWH", "src_table": "DWH.FACT_SALES",
    })
    mapping_generator.MappingGenerator()._preflight_odi_repository(params)
    assert run.call_args.args[1]["odi.source_model_code"] == "TGT_DWH"
    assert params["_odi_source_identity"]["model_code"] == "TGT_DWH"
    direct = odi_orchestrator.OdiOrchestrator().preflight_mapping(
        ikm_name=IKM, src_owner="DWH", src_table="FACT_SALES", approved=True,
    )
    assert direct["status"] == "passed"
    assert run.call_args.args[1]["odi.source_model_code"] == "TGT_DWH"

    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", '{"GLOBAL":{"DWH":"CUSTOM_DWH"}}')
    run.return_value = json.dumps(_proof(source={**source, "model_code": "CUSTOM_DWH"}))
    other_params = mapping_generator.MappingGenerator().validate_params({
        **PARAMS, "src_model": "ORACLE_DWH", "src_table": "DWH.FACT_SALES",
    })
    mapping_generator.MappingGenerator()._preflight_odi_repository(other_params)
    assert run.call_args.args[1]["odi.source_model_code"] == "CUSTOM_DWH"
    assert other_params["_odi_source_identity"]["model_code"] == "CUSTOM_DWH"

    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", '{"GLOBAL":{"DM":"TGT_DM"}}')
    run.return_value = json.dumps({
        "tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False,
        "repository_connection_attempted": True, "code": "source_identity_ambiguous",
    })
    unbound = odi_orchestrator.OdiOrchestrator().preflight_mapping(
        ikm_name=IKM, src_owner="DWH", src_table="FACT_SALES", approved=True,
    )
    assert unbound["code"] == "source_identity_ambiguous"
    assert "odi.source_model_code" not in run.call_args.args[1]

    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", "{")
    run.reset_mock()
    blocked = odi_orchestrator.OdiOrchestrator().preflight_mapping(
        ikm_name=IKM, src_owner="DWH", src_table="FACT_SALES", approved=True,
    )
    assert blocked["code"] == "source_model_config_invalid"
    run.assert_not_called()


def test_missing_own_source_registers_once_before_target_ddl(monkeypatch):
    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", config.effective_source_model_bindings(None))
    plan = _plan()
    ddl_log = []
    orchestrator = SourceOrchestrator(plan)
    original = orchestrator.register_datastore

    def observe_register(**kwargs):
        assert not any(sql.startswith("CREATE") for sql in ddl_log)
        return original(**kwargs)

    orchestrator.register_datastore = observe_register
    payload, status = scd2_service.generate(
        REQUEST,
        _own_services(orchestrator, plan, ddl_log=ddl_log),
        approved=True,
    )
    assert status == 200 and payload["status"] == "generated"
    assert [step["step"] for step in payload["steps"]][:3] == [
        "odi_probe", "odi_source_datastore_register", "execution_identity",
    ]
    calls = [call[1] for call in orchestrator.calls if call[0] == "source_register"]
    assert len(calls) == 1
    assert calls[0]["model_code"] == "TGT_DWH" and calls[0]["absent_only"] is True
    assert len(calls[0]["columns"]) == len(SOURCE_COLUMNS)
    assert calls[0]["columns"][1] == {
        "name": "CUST_NAME", "type": "VARCHAR2", "length": 50,
        "semantics": "BYTE", "nullable": False,
    }
    assert any(sql.startswith("CREATE TABLE") for sql in ddl_log)
    assert payload["known_side_effects"] is True


@pytest.mark.parametrize("outcome,unknown,manual", [
    ("rolled_back", False, False),
    ("commit_outcome_unknown", True, True),
    ("rollback_failed", True, True),
])
def test_source_registration_failure_stops_before_target_ddl(monkeypatch, outcome, unknown, manual):
    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", config.effective_source_model_bindings(None))
    plan = _plan()
    orchestrator = SourceOrchestrator(plan, source_output=_source_failure(outcome))
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, _own_services(orchestrator, plan, ddl_log=ddl_log), approved=True,
    )
    assert status == 502 and payload["status"] == "partial_failure_source_datastore_register"
    assert payload["steps"][-1]["repository_outcome"] == outcome
    assert payload["outcome_unknown"] is unknown
    assert payload["manual_review_required"] is manual
    assert not any(sql.startswith(("CREATE", "GRANT")) for sql in ddl_log)
    assert "scd2_datastore_register.groovy" not in orchestrator.order


def test_foreign_source_still_blocks_and_present_own_source_skips(monkeypatch):
    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", config.effective_source_model_bindings(None))
    plan = _plan()
    missing = probe_payload(source_datastore_exists=False)
    foreign = SourceOrchestrator(plan, responses={"scd2_probe.groovy": missing})
    ddl_log = []
    body, code = scd2_service.generate(
        REQUEST, make_services(foreign, ddl_log=ddl_log), approved=True,
    )
    assert code == 409 and body["status"] == "blocked_source_datastore_missing"
    assert "source_register" not in foreign.order and ddl_log == []

    present = SourceOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload()})
    body, code = scd2_service.generate(REQUEST, _own_services(present, plan), approved=True)
    assert code == 200 and body["status"] == "generated"
    assert "source_register" not in present.order


def test_source_must_be_physical_and_trusted_before_registration(monkeypatch):
    plan = _plan()
    missing = probe_payload(source_datastore_exists=False)
    orchestrator = SourceOrchestrator(plan, responses={"scd2_probe.groovy": missing})
    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", config.effective_source_model_bindings(None))
    ddl_log = []
    body, code = scd2_service.generate(
        REQUEST, _own_services(orchestrator, plan, ddl_log=ddl_log, table_exists=False), approved=True,
    )
    assert code == 404 and body["status"] == "blocked_source_not_found"
    assert "source_register" not in orchestrator.order
    assert not any(sql.startswith("CREATE") for sql in ddl_log)

    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", '{"GLOBAL":{"DWH":"OTHER_MODEL"}}')
    orchestrator = SourceOrchestrator(plan, responses={"scd2_probe.groovy": missing})
    body, code = scd2_service.generate(REQUEST, _own_services(orchestrator, plan), approved=True)
    assert code == 409 and body["status"] == "blocked_source_model_binding_mismatch"
    assert "source_register" not in orchestrator.order

    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", "{")
    orchestrator = SourceOrchestrator(plan, responses={"scd2_probe.groovy": missing})
    body, code = scd2_service.generate(REQUEST, _own_services(orchestrator, plan), approved=True)
    assert code == 409 and body["status"] == "source_model_config_invalid"
    assert "source_register" not in orchestrator.order


def test_classic_registrar_exposes_absent_only_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(
        odi_orchestrator.OdiOrchestrator, "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "synthetic",
    )
    odi_orchestrator.OdiOrchestrator().register_datastore(
        "TGT_DWH", "SOURCE_TABLE", [{"name": "ID", "type": "NUMBER"}], absent_only=True,
    )
    assert calls == [("register_datastore.groovy", {
        "odi.model_code": "TGT_DWH", "odi.table_name": "SOURCE_TABLE",
        "odi.resource_name": "SOURCE_TABLE", "odi.columns": "ID:NUMBER:0:0::Y",
        "odi.absent_only": "true",
    })]
    script = (Path(__file__).resolve().parents[1] / "odi_automation" / "register_datastore.groovy").read_text(encoding="utf-8")
    assert script.index('if (absentOnly) throw new IllegalStateException("SourceDatastoreAlreadyExists")') < script.index("em.remove(existing)")


def test_source_registrar_transport_preserves_reviewed_failure(monkeypatch):
    from tests.test_scd2_groovy_scripts import _Completed, _prepare_runtime

    expected = _source_failure("rolled_back")
    _prepare_runtime(monkeypatch, _Completed(stdout=expected))
    with odi_orchestrator.groovy_approval_context(True):
        assert odi_orchestrator.OdiOrchestrator().run_groovy("register_datastore.groovy", {}) == expected
    unsafe = _source_failure("rolled_back") + '\n{"status":"error"}'
    _prepare_runtime(monkeypatch, _Completed(stdout=unsafe))
    with odi_orchestrator.groovy_approval_context(True):
        assert odi_orchestrator.OdiOrchestrator().run_groovy("register_datastore.groovy", {}).endswith(
            "error_type=GroovyFatalOutput"
        )


def test_unusable_source_registration_output_requires_manual_review(monkeypatch):
    monkeypatch.setattr(config, "ODI_SOURCE_MODEL_BINDINGS", config.effective_source_model_bindings(None))
    plan = _plan()
    orchestrator = SourceOrchestrator(plan, source_output="Hata: error_type=RemoteTimeout")
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, _own_services(orchestrator, plan, ddl_log=ddl_log), approved=True,
    )
    assert status == 502 and payload["outcome_unknown"] is True
    assert payload["manual_review_required"] is True
    assert payload["steps"][-1]["repository_outcome"] == "unverified"
    assert not any(sql.startswith("CREATE") for sql in ddl_log)
