"""Offline parity and checkpoint contract for the graph audit handover."""

from __future__ import annotations

import copy
import json

import pandas as pd
import pytest

import mapping_graph
import target_table_service as target_service
from mapping_generator import MappingGenerator, _canonical_audit_type
from odi_orchestrator import OdiOrchestrator


SOURCE_COLUMNS = [
    {"COLUMN_NAME": "ENTITY_ID", "ORACLE_TYPE": "NUMBER(10)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "ATTR_TEXT", "ORACLE_TYPE": "VARCHAR2(40)", "IS_NULLABLE": "Y"},
]


def _params(table="ODS_ODI90_A", *, mode="IU", source_owner="DWH"):
    return {
        "mapping_name": f"MAP_{table}",
        "src_model": "ORACLE_DWH" if source_owner == "DWH" else "SRC_HR",
        "src_owner": source_owner,
        "src_table": "SRC_ODI90",
        "tgt_model": "ORACLE_ODS",
        "tgt_owner": "DWH",
        "tgt_table": table,
        "integration_type": mode,
        "update_key_columns": ["ENTITY_ID"] if mode == "IU" else [],
        "update_key_kind": "primary",
        "needs_skey": False,
        "drop_target_if_exists": False,
        "replace_existing_mapping": False,
        "context": "GLOBAL",
    }


@pytest.fixture
def stack(monkeypatch):
    """Keep the real validation, target executor and mapping call on synthetic catalog data."""
    catalog = {"exists": {}, "types": {}}
    calls = {"mapping": [], "private": [], "events": [], "register": 0}

    monkeypatch.setattr(MappingGenerator, "_source_columns_for", lambda _self, _params: copy.deepcopy(SOURCE_COLUMNS))

    def target_exists(context):
        calls["events"].append("target_exists")
        return catalog["exists"].get(context.table, False)

    def read_schema(_owner, table):
        calls["events"].append("read_schema")
        return pd.DataFrame(
            [{"COLUMN_NAME": name, "FULL_DATA_TYPE": kind} for name, kind in catalog["types"][table].items()]
        )

    def create_target(context, _definitions):
        calls["events"].append("create_target")
        catalog["exists"][context.table] = True
        return "CREATE TABLE synthetic"

    monkeypatch.setattr(target_service, "_target_exists", target_exists)
    monkeypatch.setattr(target_service, "read_table_schema", read_schema)
    monkeypatch.setattr(target_service, "_target_columns", lambda context: set(catalog["types"][context.table]))
    monkeypatch.setattr(target_service, "_verify_key_on_existing", lambda *_args: {"status": "ALREADY_PRESENT"})
    monkeypatch.setattr(target_service, "_create_target", create_target)
    monkeypatch.setattr(target_service, "_drop_target", lambda context: catalog["exists"].update({context.table: False}))
    monkeypatch.setattr(target_service, "_apply_grants", lambda *_args: None)
    monkeypatch.setattr(target_service, "_write_creation_audit", lambda *_args: None)

    def preflight(_self, params):
        calls["events"].append("preflight")
        params["_odi_ikm_id"] = "7"
        params["_odi_source_identity"] = {"model_code": "SRC_MODEL", "datastore_exists": False}
        return {"status": "passed"}

    def bindings(_self, params):
        calls["events"].append("bindings")
        params.update({
            "_odi_repository": {"mapping_exists": False},
            "_odi_src_model_code": "SRC_MODEL",
            "_odi_tgt_model_code": "TGT_MODEL",
            "_odi_src_datastore_name": "SRC_ODI90",
            "_odi_tgt_datastore_name": params["tgt_table"],
        })
        return params["_odi_repository"]

    def register(self, params):
        calls["register"] += 1
        self._ensure_odi_repository_bindings(params)
        return {"src_result": "OK", "tgt_result": "OK"}

    real_create_mapping = MappingGenerator._create_odi_mapping

    def create_mapping(self, params):
        calls["private"].append({
            "expressions": dict(self._audit_preservation_expressions),
            "alias": self._audit_lookup_alias,
            "public": dict(self._audit_preservation_public),
            "snapshot": self._target_audit_snapshot,
            "integration_type": params["integration_type"],
            "key_columns": list(params["update_key_columns"]),
        })
        return real_create_mapping(self, params)

    monkeypatch.setattr(MappingGenerator, "_preflight_odi_repository", preflight)
    monkeypatch.setattr(MappingGenerator, "_resolve_odi_repository_bindings", bindings)
    monkeypatch.setattr(MappingGenerator, "_register_odi_datastores", register)
    monkeypatch.setattr(MappingGenerator, "_create_odi_mapping", create_mapping)
    monkeypatch.setattr(OdiOrchestrator, "generate_mapping", lambda _self, **kwargs: calls["mapping"].append(kwargs) or "OK")
    for method in ("_populate_lineage", "_generate_tests", "_take_snapshot"):
        monkeypatch.setattr(MappingGenerator, method, lambda _self, _params: {})
    monkeypatch.setattr(MappingGenerator, "_log_to_mapping_log", lambda *_args: None)
    monkeypatch.setattr(mapping_graph, "_persist_graph_state", lambda *_args: None)

    def configure(params, *, exists=False, declared=None):
        validated = MappingGenerator().validate_params(params)
        plan = MappingGenerator()._build_target_column_plan(validated)
        types = {name: _canonical_audit_type(kind) for name, kind in plan["desired_col_defs"]}
        types.update(declared or {})
        catalog["types"][validated["tgt_table"]] = types
        catalog["exists"][validated["tgt_table"]] = exists
        return validated

    return catalog, calls, configure


@pytest.mark.parametrize("case", ["new", "reused", "truncate", "no_audit"])
def test_graph_and_classic_send_identical_mapping_creation_arguments(stack, case):
    _catalog, calls, configure = stack
    params = _params(mode="CONTROL_APPEND" if case == "truncate" else "IU",
                     source_owner="HR" if case == "no_audit" else "DWH")
    configure(params, exists=case == "reused",
              declared={"ETL_BATCH_ID": "VARCHAR2(20)"} if case == "reused" else None)

    classic = MappingGenerator().generate_from_params(copy.deepcopy(params))
    assert classic["overall_status"] == "SUCCESS", classic.get("error")
    classic_call, classic_private = calls["mapping"][-1], calls["private"][-1]
    calls["events"].clear()
    # A new target is already present after the classic run; reset the fake catalog.
    if case != "reused":
        stack[0]["exists"][params["tgt_table"]] = False
    graph = mapping_graph.run_mapping_graph(params=copy.deepcopy(params))
    assert graph["overall_status"] == "success", graph.get("error")
    assert "audit_handover" not in graph
    assert calls["mapping"][-1] == classic_call
    assert calls["private"][-1] == classic_private
    if case in {"new", "reused"}:
        assert classic_private["expressions"]
        assert calls["events"].index("target_exists") < calls["events"].index("preflight")
        batch_expression = classic_private["expressions"]["ETL_BATCH_ID"]
        assert "VARCHAR2(20)" in batch_expression if case == "reused" else "TO_NUMBER(" in batch_expression
    else:
        assert classic_private["expressions"] == {}
        assert classic_call["integration_type"] == ("CONTROL_APPEND" if case == "truncate" else "IU")
        assert classic_call["audit_preservation_columns"] is None


@pytest.mark.parametrize("change", ["disappear", "type_change"])
def test_graph_refuses_target_change_before_datastore_write(stack, change):
    catalog, calls, configure = stack
    params = configure(_params(), exists=True)
    state = {"validated_params": params, "batch_id": "batch", "completed_steps": [], "log": []}
    after_target = mapping_graph.node_create_table(state)
    assert after_target.get("overall_status") != "failed", after_target.get("error")
    state.update(after_target)
    if change == "disappear":
        catalog["exists"][params["tgt_table"]] = False
    else:
        catalog["types"][params["tgt_table"]]["ETL_BATCH_ID"] = "VARCHAR2(20)"
    outcome = mapping_graph.node_register_datastores(state)
    assert outcome["error_code"] == "target_changed_during_generation"
    assert calls["register"] == 0
    assert calls["mapping"] == []


def test_new_target_is_checked_again_before_datastore_write(stack):
    catalog, calls, configure = stack
    params = configure(_params(), exists=False)
    state = {"validated_params": params, "batch_id": "batch", "completed_steps": [], "log": []}
    state.update(mapping_graph.node_create_table(state))
    assert state["target_table_result"]["status"] == "CREATED"
    assert state["audit_handover"]["snapshot"]["exists"] is False
    assert state["audit_handover"]["post_target_snapshot"]["exists"] is True
    catalog["types"][params["tgt_table"]]["ETL_BATCH_ID"] = "VARCHAR2(20)"
    outcome = mapping_graph.node_register_datastores(state)
    assert outcome["error_code"] == "target_changed_during_generation"
    assert calls["register"] == 0


def test_graph_rechecks_target_immediately_before_mapping_write(stack):
    catalog, calls, configure = stack
    params = configure(_params(), exists=True)
    state = {"validated_params": params, "batch_id": "batch", "completed_steps": [], "log": []}
    state.update(mapping_graph.node_create_table(state))
    state.update(mapping_graph.node_register_datastores(state))
    assert state.get("overall_status") != "failed"
    catalog["exists"][params["tgt_table"]] = False
    outcome = mapping_graph.node_create_mapping(state)
    assert outcome["error_code"] == "target_changed_during_generation"
    assert calls["mapping"] == []


def test_graph_checkpoint_round_trip_restores_or_fails_closed(stack):
    _catalog, calls, configure = stack
    params = configure(_params(), exists=True)
    state = {"validated_params": params, "batch_id": "batch", "completed_steps": [], "log": []}
    state.update(mapping_graph.node_create_table(state))
    handover = state["audit_handover"]
    assert json.loads(json.dumps(handover)) == handover
    assert set(handover) == {
        "version", "target", "integration_type", "update_key_columns", "expressions",
        "lookup_alias", "public", "types_decided", "snapshot", "post_target_snapshot",
    }
    resumed = json.loads(json.dumps(state))
    resumed.update(mapping_graph.node_register_datastores(resumed))
    assert resumed.get("overall_status") != "failed", resumed.get("error")
    outcome = mapping_graph.node_create_mapping(resumed)
    assert outcome.get("overall_status") != "failed", outcome.get("error")
    assert calls["private"][-1]["expressions"]

    missing = json.loads(json.dumps(state))
    missing.pop("audit_handover", None)
    before = calls["register"]
    blocked = mapping_graph.node_register_datastores(missing)
    assert blocked["error_code"] == "graph_audit_state_missing"
    assert calls["register"] == before

    partial = json.loads(json.dumps(state))
    partial["audit_handover"]["snapshot"].pop("declared_types")
    blocked = mapping_graph.node_register_datastores(partial)
    assert blocked["error_code"] == "graph_audit_state_missing"
    assert calls["register"] == before


@pytest.mark.parametrize("remove_handover", [False, True])
def test_memory_checkpoint_resume_uses_complete_handover_or_blocks(stack, remove_handover):
    _catalog, calls, configure = stack
    params = configure(_params(), exists=True)
    graph = mapping_graph.get_graph(use_memory_checkpoint=True)
    config = {"configurable": {"thread_id": "odi90-resume"}}
    list(graph.stream({"raw_params": copy.deepcopy(params)}, config=config, interrupt_after=["create_table"]))
    checkpoint = graph.get_state(config)
    assert checkpoint.next == ("register_datastores",)
    assert json.loads(json.dumps(checkpoint.values["audit_handover"])) == checkpoint.values["audit_handover"]
    if remove_handover:
        graph.update_state(config, {"audit_handover": None}, as_node="create_table")
    list(graph.stream(None, config=config))
    final = graph.get_state(config).values
    if remove_handover:
        assert final["overall_status"] == "failed"
        assert final["error_code"] == "graph_audit_state_missing"
        assert calls["register"] == 0
        assert calls["mapping"] == []
    else:
        assert final["overall_status"] == "success", final.get("error")
        assert calls["private"][-1]["expressions"]


def test_graph_requests_keep_distinct_audit_decisions(stack):
    _catalog, calls, configure = stack
    first = _params("ODS_ODI90_A")
    second = _params("ODS_ODI90_B")
    configure(first, exists=True)
    configure(second, exists=True)
    for params in (first, second):
        result = mapping_graph.run_mapping_graph(params=copy.deepcopy(params))
        assert result["overall_status"] == "success", result.get("error")
    prior, later = calls["private"][-2:]
    assert prior["expressions"] and later["expressions"]
    assert '"ODS_ODI90_A"' in prior["expressions"]["INSERT_DATE"]
    assert '"ODS_ODI90_B"' in later["expressions"]["INSERT_DATE"]
    assert '"ODS_ODI90_A"' not in later["expressions"]["INSERT_DATE"]
    assert prior["snapshot"].table == "ODS_ODI90_A"
    assert later["snapshot"].table == "ODS_ODI90_B"
