"""Offline route probes for exception text produced below the HTTP redactor."""

import json

import oracledb
import pytest


MARKER = "ODI94_SYNTHETIC_PRIVATE_MARKER"


def _body(response):
    body = response.get_json()
    assert MARKER not in json.dumps(body)
    return body


def _graph_route(app_client, monkeypatch, producer, *, path="/api/mapping/generate-graph"):
    import app as app_module

    monkeypatch.setattr(app_module, "run_mapping_graph", lambda **_kwargs: producer())
    response = app_client.post(path, json={"params": {"mapping_name": "MAP_X"}, "approved": True})
    assert response.status_code == 200
    return _body(response)


@pytest.mark.parametrize(
    "node,method,step,sentence",
    [
        ("node_parse_prompt", "parse_prompt", "parse_prompt", "Prompt parsing failed."),
        ("node_validate", "validate_params", "validate", "Validation failed."),
        ("node_create_table", "_decide_effective_audit", "create_table", "Table creation failed."),
        ("node_register_datastores", "_register_odi_datastores", "register_datastores", "Datastore registration failed."),
        ("node_create_mapping", "_create_odi_mapping", "create_mapping", "Mapping creation failed."),
        ("node_create_skey", "_create_skey", "create_skey", "SKEY creation failed."),
    ],
)
def test_graph_node_failure_producer_and_route(app_client, monkeypatch, node, method, step, sentence):
    import mapping_generator
    import mapping_graph

    class FakeGenerator:
        def __getattr__(self, name):
            if name == method:
                def fail(*_args, **_kwargs):
                    raise ValueError(MARKER)
                return fail
            raise AttributeError(name)

    monkeypatch.setattr(mapping_generator, "MappingGenerator", FakeGenerator)
    monkeypatch.setattr(mapping_graph, "_restore_audit_handover", lambda *_args: None)
    monkeypatch.setattr(mapping_graph, "_guard_post_target", lambda *_args: None)
    state = {"user_prompt": "please map", "raw_params": {"mapping_name": "MAP_X"},
             "validated_params": {"mapping_name": "MAP_X"}, "batch_id": "test", "log": []}
    producer = lambda: getattr(mapping_graph, node)(state)
    direct = producer()
    if node == "node_create_skey":
        assert direct["skey_result"]["error"] == sentence
        assert direct["log"][-1]["status"] == "WARNING"
    else:
        assert direct["error"] == sentence
        assert direct["overall_status"] == "failed"
        assert direct["log"][-1]["status"] == "FAILED"
    assert direct["log"][-1]["step"] == step
    assert direct["log"][-1]["detail"] == sentence
    body = _graph_route(app_client, monkeypatch, producer)
    assert body["current_step"] == step
    assert body["log"][-1]["step"] == step
    if node == "node_create_skey":
        assert body["log"][-1]["status"] == "WARNING"
        assert body["log"][-1]["detail"] == sentence
        default_body = _graph_route(app_client, monkeypatch, producer, path="/api/mapping/generate")
        assert default_body["log"][-1]["detail"] == sentence
    else:
        assert body["overall_status"] == "failed"
        assert body["log"][-1]["status"] == "FAILED"


@pytest.mark.parametrize("method,key,step,sentence", [
    ("_populate_lineage", "lineage_result", "lineage", "Lineage population failed."),
    ("_generate_tests", "test_result", "tests", "DQ test generation failed."),
    ("_take_snapshot", "snapshot_result", "snapshot", "Mapping snapshot failed."),
])
def test_graph_post_step_exception_producer_and_route(app_client, monkeypatch, method, key, step, sentence):
    import mapping_generator
    import mapping_graph

    class FakeGenerator:
        def __getattr__(self, name):
            if name == method:
                def fail(*_args):
                    raise RuntimeError(MARKER)
                return fail
            if name == "_populate_lineage":
                return lambda *_args: {"lineage_result": {"inserted": 2}}
            if name == "_generate_tests":
                return lambda *_args: {"tests": ["TEST_X"]}
            if name == "_take_snapshot":
                return lambda *_args: {"snap_id": 7}
            raise AttributeError(name)

    monkeypatch.setattr(mapping_generator, "MappingGenerator", FakeGenerator)
    producer = lambda: mapping_graph.node_post_steps({"validated_params": {}, "batch_id": "test", "log": []})
    direct = producer()
    assert direct[key]["error"] == sentence
    item = next(item for item in direct["log"] if item["step"] == step)
    assert item["status"] == "WARNING"
    assert item["detail"] == sentence
    body = _graph_route(app_client, monkeypatch, producer, path="/api/mapping/generate")
    item = next(item for item in body["log"] if item["step"] == step)
    assert item["status"] == "WARNING"
    assert item["detail"] == sentence


@pytest.mark.parametrize("method,key,step,sentence", [
    ("_populate_lineage", "lineage_result", "lineage", "Lineage population failed."),
    ("_generate_tests", "test_result", "tests", "DQ test generation failed."),
    ("_take_snapshot", "snapshot_result", "snapshot", "Mapping snapshot failed."),
])
def test_generator_nested_failure_reaches_graph_route(app_client, monkeypatch, method, key, step, sentence):
    import mapping_generator
    import mapping_graph

    if method == "_populate_lineage":
        import lineage_manager

        class FakeLineage:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def populate_from_odi(self):
                raise RuntimeError(MARKER)

        monkeypatch.setattr(lineage_manager, "LineageManager", FakeLineage)
    elif method == "_generate_tests":
        import test_generator

        class FakeTests:
            def generate_and_store_in_rag(self, **_kwargs):
                raise RuntimeError(MARKER)

            def close(self):
                pass

        monkeypatch.setattr(test_generator, "TestGenerator", FakeTests)
    else:
        import mapping_snapshot_manager

        class FakeSnapshot:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def ensure_table(self):
                raise RuntimeError(MARKER)

        monkeypatch.setattr(mapping_snapshot_manager, "MappingSnapshotManager", FakeSnapshot)

    for name, output in (("_populate_lineage", {"lineage_result": {"inserted": 2}}),
                         ("_generate_tests", {"tests": []}), ("_take_snapshot", {"snap_id": 7})):
        if name != method:
            monkeypatch.setattr(mapping_generator.MappingGenerator, name, lambda *_args, value=output: value)
    params = {"mapping_name": "MAP_X", "src_model": "ORACLE_SOURCE", "src_table": "SRC_X",
              "tgt_model": "ORACLE_ODS", "tgt_table": "ODS_X", "tgt_owner": "DWH", "needs_skey": False}
    producer = lambda: mapping_graph.node_post_steps({"validated_params": params, "batch_id": "test", "log": []})
    direct = producer()
    nested = direct[key]
    if method == "_populate_lineage":
        nested = nested["lineage_result"]
    assert nested["error"] == sentence
    body = _graph_route(app_client, monkeypatch, producer)
    assert next(item for item in body["log"] if item["step"] == step)["status"] == "SUCCESS"


@pytest.mark.parametrize("side", ["source", "target"])
def test_datastore_warning_string_and_graph_detail(app_client, monkeypatch, side):
    import mapping_generator
    import mapping_graph
    import odi_orchestrator
    import target_table_service

    generator = mapping_generator.MappingGenerator
    monkeypatch.setattr(generator, "_ensure_odi_repository_bindings", lambda *_args: None)
    monkeypatch.setattr(generator, "_source_columns_for", lambda *_args: [] if side == "target" else (_ for _ in ()).throw(RuntimeError(MARKER)))
    monkeypatch.setattr(generator, "_odi_register_column_defs", lambda *_args: [])
    monkeypatch.setattr(generator, "_resolved_odi_source_model_code", lambda *_args: "SRC")
    monkeypatch.setattr(generator, "_resolved_odi_target_model_code", lambda *_args: "TGT")
    monkeypatch.setattr(generator, "_source_schema_df_to_legacy_columns", lambda *_args: [])
    monkeypatch.setattr(mapping_generator, "get_source_connector", lambda *_args: None)
    monkeypatch.setattr(target_table_service, "read_table_schema",
                        lambda *_args: [] if side == "source" else (_ for _ in ()).throw(RuntimeError(MARKER)))
    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "register_datastore", lambda *_args, **_kwargs: "registered")
    monkeypatch.setattr(mapping_graph, "_restore_audit_handover", lambda *_args: None)
    monkeypatch.setattr(mapping_graph, "_guard_post_target", lambda *_args: None)
    params = {"src_model": "ORACLE_SOURCE", "src_table": "SRC_X", "tgt_model": "ORACLE_ODS",
              "tgt_table": "ODS_X", "tgt_owner": "DWH"}
    producer = lambda: mapping_graph.node_register_datastores({"validated_params": params, "batch_id": "test", "log": []})
    direct = producer()
    field = "src_result" if side == "source" else "tgt_result"
    assert direct["datastore_result"][field] == f"WARNING: {'Source' if side == 'source' else 'Target'} datastore registration failed."
    assert direct["log"][-1]["status"] == "WARNING"
    assert direct["log"][-1]["detail"] == "ODI datastore registration warning."
    body = _graph_route(app_client, monkeypatch, producer)
    assert body["datastore_result"][field] == direct["datastore_result"][field]
    assert body["log"][-1]["detail"] == "ODI datastore registration warning."
    default_body = _graph_route(app_client, monkeypatch, producer, path="/api/mapping/generate")
    assert default_body["log"][-1]["detail"] == "ODI datastore registration warning."

    def generate_from_params(self, raw_params):
        registered = self._register_odi_datastores(raw_params)
        return {"overall_status": "PARTIAL", "steps": {"register_datastores": registered},
                "log": self._log_entries}

    monkeypatch.setattr(generator, "generate_from_params", generate_from_params)
    classic = app_client.post("/api/mapping/generate?engine=classic", json={"params": params, "approved": True})
    assert classic.status_code == 200
    classic_body = _body(classic)
    assert classic_body["steps"]["register_datastores"][field] == direct["datastore_result"][field]


def test_partial_skey_log_on_update_route(app_client, monkeypatch):
    import app as app_module
    import mapping_generator
    import skey_manager

    class FakeSkey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def create_skey_objects(self, **_kwargs):
            return {"errors": [{"object": "SKEY_MAP_X", "error": MARKER}], "created": [], "skipped": []}

        def get_skey_expression(self, **_kwargs):
            return "SAFE_EXPR"

    monkeypatch.setattr(skey_manager, "SkeyManager", FakeSkey)

    def update(self, *_args):
        result = self._create_skey({"tgt_owner": "DWH", "tgt_table": "X", "skey_natural_key": "SRC.ID"})
        return {"overall_status": "PARTIAL", "steps": result, "log": self._log_entries}

    monkeypatch.setattr(mapping_generator.MappingGenerator, "update_mapping", update)
    response = app_client.post("/api/mapping/update", json={"mapping_name": "MAP_X", "changes": {"x": 1}, "approved": True})
    assert response.status_code == 200
    body = _body(response)
    assert body["overall_status"] == "PARTIAL"
    assert body["log"][0]["step"] == "create_skey"
    assert body["log"][0]["status"] == "PARTIAL"
    assert body["log"][0]["detail"] == "SKEY object creation partially failed."


def test_analyze_prompt_parse_exception_route(app_client, monkeypatch):
    import mapping_generator

    def fail(self, _prompt):
        raise ValueError(MARKER)

    monkeypatch.setattr(mapping_generator.MappingGenerator, "parse_prompt", fail)
    direct = mapping_generator.MappingGenerator().analyze_prompt("map X")
    assert direct["validation"]["error"] == "Prompt parsing failed."
    response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
    assert response.status_code == 200
    body = _body(response)
    assert body["validation"]["status"] == "INVALID"
    assert body["validation"]["error"] == "mapping prompt analysis failed."


def test_analyze_prompt_incomplete_model_output_is_not_copied(app_client, monkeypatch):
    import ai_agent
    import mapping_generator

    monkeypatch.setattr(ai_agent, "call_llm", lambda *_args: MARKER)
    monkeypatch.setattr(ai_agent, "extract_json", lambda *_args: {"mapping_name": "MAP_X"})
    direct = mapping_generator.MappingGenerator().analyze_prompt("map X")
    assert direct["validation"]["error"] == "Prompt parsing failed."
    response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
    assert response.status_code == 200
    assert _body(response)["validation"]["status"] == "INVALID"


def test_analyze_prompt_keeps_application_validation_sentence(app_client, monkeypatch):
    import mapping_generator

    monkeypatch.setattr(mapping_generator.MappingGenerator, "parse_prompt",
                        lambda *_args: {"mapping_name": "MAP_X", "src_table": "", "tgt_table": "ODS_X"})
    response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
    assert response.status_code == 200
    body = _body(response)
    assert body["validation"]["status"] == "INVALID"
    assert body["validation"]["error"] == "src_table cannot be empty."
    assert body["validation"]["error_type"] == "ValueError"
    assert "_public_validation" not in body


def test_analyze_prompt_untrusted_validation_exception_is_fixed(app_client, monkeypatch):
    import mapping_generator

    monkeypatch.setattr(mapping_generator.MappingGenerator, "parse_prompt",
                        lambda *_args: {"mapping_name": "MAP_X"})

    def fail(*_args):
        raise ValueError(MARKER)

    monkeypatch.setattr(mapping_generator.MappingGenerator, "validate_params", fail)
    direct = mapping_generator.MappingGenerator().analyze_prompt("map X")
    assert direct["validation"]["error"] == "Parameter validation failed."
    response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
    assert response.status_code == 200
    assert _body(response)["validation"]["error"] == "mapping prompt analysis failed."


@pytest.mark.parametrize("phase", ["expression", "connection"])
def test_validate_expression_route(app_client, monkeypatch, phase):
    import db

    class FakeCursor:
        def execute(self, _sql):
            raise RuntimeError(MARKER)

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(db, "get_connection", lambda: FakeConn() if phase == "expression" else (_ for _ in ()).throw(RuntimeError(MARKER)))
    response = app_client.post("/api/mapping/validate-expression", json={"target_table": "ODS_X", "expressions": {"X": "SYSDATE"}})
    assert response.status_code == 200
    body = _body(response)
    if phase == "expression":
        assert body["valid"] is False
        assert body["errors"] == {"X": "Expression validation failed."}
    else:
        assert body["valid"] is True
        assert body["warning"] == "DB validation skipped."


def test_validate_expression_success_keeps_base_shape(app_client, monkeypatch):
    import db

    class FakeCursor:
        def execute(self, _sql):
            pass

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(db, "get_connection", lambda: FakeConn())
    response = app_client.post("/api/mapping/validate-expression",
                               json={"target_table": "ODS_X", "expressions": {"X": "SYSDATE"}})
    assert response.status_code == 200
    assert response.get_data() == b'{"valid":true}\n'


@pytest.mark.parametrize("object_kind", ["table", "sequence"])
def test_skey_create_route(app_client, monkeypatch, object_kind):
    import app as app_module
    import skey_manager

    class FakeCursor:
        def execute(self, sql, *_args):
            if ("CREATE TABLE" in sql) == (object_kind == "table"):
                raise oracledb.DatabaseError(MARKER)

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(skey_manager.SkeyManager, "_get_conn", lambda _self: FakeConn())
    monkeypatch.setattr(skey_manager.SkeyManager, "_log_operation", lambda *_args: None)
    monkeypatch.setattr(skey_manager.SkeyManager, "_auto_grant", lambda *_args: None)
    response = app_client.post("/api/skey/create", json={"owner": "DWH", "table": "X", "approved": True})
    assert response.status_code == 200
    body = _body(response)
    assert body["errors"][0]["error"] == "SKEY object creation failed."
    assert body["errors"][0]["object"] == ("SKEY_MAP_X" if object_kind == "table" else "SEQ_X")


@pytest.mark.parametrize("exception_type", [oracledb.DatabaseError, RuntimeError])
def test_quality_bulk_route(app_client, monkeypatch, exception_type):
    import data_quality

    class FakeCursor:
        def execute(self, *_args):
            pass

        def fetchall(self):
            return [("T_X",)]

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(data_quality.DataQualityManager, "_get_audit_conn", lambda _self: FakeConn())

    def fail(*_args):
        raise exception_type(MARKER)

    monkeypatch.setattr(data_quality.DataQualityManager, "create_error_table", fail)
    response = app_client.post("/api/quality/create-all-error-tables", json={"owner": "DWH", "approved": True})
    assert response.status_code == 200
    body = _body(response)
    assert body["errors"] == [{"table": "T_X", "error": "Quality error table creation failed.",
                               "error_type": exception_type.__name__}]


def test_ask_modeled_table_missing_route(app_client, monkeypatch):
    import app as app_module
    import ai_helpers

    monkeypatch.setattr(app_module, "_is_modeled_tables_question", lambda *_args: False)
    monkeypatch.setattr(app_module, "_is_llm_status_question", lambda *_args: False)
    monkeypatch.setattr(app_module, "_is_truncate_mapping_intent", lambda *_args: False)
    monkeypatch.setattr(app_module, "_is_remembered_unmapped_flow_mapping_request", lambda *_args: False)
    monkeypatch.setattr(app_module, "_is_unmapped_odi_flow_question", lambda *_args: False)
    monkeypatch.setattr(app_module, "_is_table_data_request", lambda *_args: True)
    monkeypatch.setattr(app_module, "_table_data_answer", ai_helpers._table_data_answer)
    monkeypatch.setattr(ai_helpers, "_extract_table_ref_from_text", lambda *_args: "DWH.ODS_X")
    monkeypatch.setattr(ai_helpers, "_modeled_target_lookup", lambda: {"DWH.ODS_X": {"MAPPING_NAME": "MAP_X", "SOURCE_TABLES": "SRC.X"}})
    monkeypatch.setattr(ai_helpers, "_build_table_preview_sql", lambda *_args: "SELECT 1 FROM DWH.ODS_X")
    monkeypatch.setattr(ai_helpers, "execute_select", lambda *_args: (_ for _ in ()).throw(RuntimeError("ORA-00942: " + MARKER)))
    direct = ai_helpers._table_data_answer("show table", [])
    assert direct is not None
    response = app_client.post("/api/ask", json={"message": "show table", "history": []})
    assert response.status_code == 200
    body = _body(response)
    assert body["queries"][0]["error"] == "Modeled target table is not available."
    assert body["queries"][0]["error_type"] == "RuntimeError"
    assert body["queries"][0]["result"]["rows"][0]["MAPPING_NAME"] == "MAP_X"


@pytest.mark.parametrize("probe_status", ["error", "passed"])
def test_scenario_probe_route_discards_groovy_detail(app_client, monkeypatch, probe_status):
    import odi_orchestrator

    class FakeOrchestrator(odi_orchestrator.OdiOrchestrator):
        def generate_scenario_metadata(self, **_kwargs):
            return {"status": "success"}

        def run_groovy(self, *_args):
            payload = {"status": probe_status, "error_detail": MARKER,
                       "checks": [{"name": "scenario_probe_exception", "status": "fail",
                                   "detail": "ODI scenario probe failed before completion."}]}
            if probe_status == "error":
                payload["error"] = "RuntimeException"
            return json.dumps(payload)

    monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOrchestrator)
    response = app_client.post("/api/schema/change/reconcile-scenario", json={"approved": True, "mapping_name": "MAP_X"})
    assert response.status_code == 200
    body = _body(response)
    assert body["status"] == "SCENARIO_RECONCILED"
    assert body["probe"]["status"] == probe_status
    assert body["probe"]["error_detail"] == "ODI scenario probe failed."
    if probe_status == "error":
        assert body["probe"]["error_type"] == "RuntimeException"


@pytest.mark.parametrize("operation,failed", [
    ("classic", False), ("classic", True),
    ("graph", False), ("graph", True),
    ("generate-graph", False), ("generate-graph", True),
    ("update", False), ("update", True),
    ("analyze", False), ("analyze", True),
    ("expression", False), ("expression", True),
])
def test_route_golden_legitimate_fields(app_client, monkeypatch, operation, failed):
    """Embedded d02c060 route shapes; only the named exception slots may change."""
    import app as app_module
    import odi_orchestrator

    log = [{"step": "create_table", "status": "CREATED", "detail": "ODS_X: CREATED; 3 source columns"},
           {"step": "create_skey", "status": "SKIPPED", "detail": "SKEY not requested"}]
    result = {"batch_id": "batch-1", "mapping_name": "MAP_X", "overall_status": "success",
              "log": log, "steps": {"create_table": {"status": "CREATED", "table": "ODS_X",
                                                     "columns_count": 3}}}
    if failed:
        result = {"batch_id": "batch-1", "mapping_name": "MAP_X", "overall_status": "failed",
                  "error": "driver detail", "log": [*log, {"step": "create_mapping", "status": "FAILED",
                                                           "detail": "driver detail"}],
                  "steps": {"create_table": {"status": "CREATED", "table": "ODS_X",
                                             "columns_count": 3}}}

    class FakeGenerator:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def generate_from_params(self, _params):
            return result

        def update_mapping(self, _name, _changes):
            return result

        def analyze_prompt(self, _prompt):
            if failed:
                return {"prompt": "map X", "params": None, "preview": None,
                        "validation": {"status": "INVALID", "error": "model detail"}}
            return {"prompt": "map X", "params": {"mapping_name": "MAP_X"},
                    "preview": {"mapping_name": "MAP_X", "flow": "SRC.X --> DWH.ODS_X",
                                "steps_to_run": ["Create target table: DWH.ODS_X", "Create ODI mapping"]},
                    "validation": {"status": "VALID", "params": {"mapping_name": "MAP_X"}}}

    monkeypatch.setattr(app_module, "MappingGenerator", FakeGenerator)
    monkeypatch.setattr(app_module, "run_mapping_graph", lambda **_kwargs: result)
    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "validate_expression",
                        lambda *_args: ({"valid": False, "errors": {"X": "driver detail"}}
                                       if failed else {"valid": True}))

    if operation == "classic":
        response = app_client.post("/api/mapping/generate?engine=classic", json={"params": {"mapping_name": "MAP_X"}, "approved": True})
        baseline = dict(result)
        if failed:
            baseline["error"] = "mapping generation failed."
            baseline["error_type"] = "OperationError"
            baseline["log"] = [*log, {"step": "create_mapping", "status": "FAILED",
                                      "detail": "mapping generation failed."}]
    elif operation in {"graph", "generate-graph"}:
        path = "/api/mapping/generate" if operation == "graph" else "/api/mapping/generate-graph"
        response = app_client.post(path, json={"params": {"mapping_name": "MAP_X"}, "approved": True})
        baseline = dict(result)
        if operation == "graph":
            baseline["engine"] = "graph"
        if failed and operation == "graph":
            baseline["error"] = "mapping generation failed."
            baseline["error_type"] = "OperationError"
            baseline["log"] = [*log, {"step": "create_mapping", "status": "FAILED",
                                      "detail": "mapping generation failed."}]
    elif operation == "update":
        response = app_client.post("/api/mapping/update", json={"mapping_name": "MAP_X", "changes": {"x": 1}, "approved": True})
        baseline = dict(result)
        if failed:
            baseline["error"] = "mapping update failed."
            baseline["error_type"] = "OperationError"
            baseline["log"] = [*log, {"step": "create_mapping", "status": "FAILED",
                                      "detail": "mapping update failed."}]
    elif operation == "analyze":
        response = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "map X"})
        baseline = FakeGenerator().analyze_prompt("map X")
    else:
        response = app_client.post("/api/mapping/validate-expression",
                                   json={"target_table": "ODS_X", "expressions": {"X": "SYSDATE"}})
        baseline = {"valid": False, "errors": {"X": "driver detail"}} if failed else {"valid": True}

    assert response.status_code == 200
    actual = response.get_json()
    if operation == "generate-graph" and failed:
        baseline["error"] = "mapping graph generation failed."
        baseline["error_type"] = "OperationError"
        baseline["log"] = [*log, {"step": "create_mapping", "status": "FAILED",
                                  "detail": "mapping graph generation failed."}]
    if operation == "analyze" and failed:
        baseline["validation"] = {"status": "INVALID", "error": "mapping prompt analysis failed.",
                                  "error_type": "OperationError"}
    assert actual == baseline
    assert response.get_data() == (app_module.app.json.dumps(baseline, separators=(",", ":")) + "\n").encode("utf-8")
