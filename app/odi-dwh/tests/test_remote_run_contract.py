"""Common remote runner: synthetic data and mocked external boundaries only."""

import ast
import json
import subprocess
from pathlib import Path
from types import CodeType, FunctionType, SimpleNamespace
from unittest.mock import Mock

import pytest

import mapping_run_service as service
import odi_orchestrator as oo
import remote_run_contract as contract
import scd2_builder
import scd2_odi
import scd2_service
from tests.scd2_support import REQUEST, SOURCE_COLUMNS, FakeOrchestrator, make_services, probe_payload


def healthy():
    return contract.result("health", status="passed", stage="completed", agent=True)


def done():
    return contract.result("run", status="passed", stage="completed", agent=True,
                           repository=True, may_have_started=True, session_status="DONE")


class RemoteFakeOrchestrator(FakeOrchestrator):
    """Adapt the existing synthetic test double without changing shared fixtures."""

    def __init__(self, *args, run_result=None, **kwargs):
        super().__init__(*args, run_result=run_result, **kwargs)
        self.remote_result = run_result if run_result is not None else done()

    def remote_agent_health(self, **kwargs):
        return healthy()

    def run_scenario_remote(self, **kwargs):
        self.calls.append(("scenario_run", kwargs))
        return self.remote_result


def generated(name="MAP_TEST_SCN", version="001"):
    return {"tool": "odi.generate_scenario_metadata", "mode": "scenario_metadata_generation",
            "status": "already_ready", "side_effects": False, "repository_connection_attempted": True,
            "scenario": {"name": name, "version": version},
            "checks": [{"name": "scenario_exact_mapping_owner", "status": "pass"}]}


@pytest.mark.parametrize("change", [
    {"session_status": None}, {"session_status": "ERROR"}, {"session_status": "RUNNING"},
    {"session_status": []}, {"status": []}, {"error_code": []}, {"stage": []},
    {"side_effects": 1}, {"agent_contact_attempted": 1}, {"repository_connection_attempted": False},
    {"run_may_have_started": False}, {"outcome_unknown": True}, {"raw_rows_included": True},
    {"pk_values_included": True}, {"secret_printed": True}, {"session_id": "LEAK"},
    {"sql": "LEAK"}, {"message": "LEAK"}, {"error_type": "LEAK"}, {"tool": "other"},
])
def test_malformed_success_never_passes_or_returns_extra_fields(change):
    parsed = contract.parse({**done(), **change}, "run")
    assert parsed["status"] == "unknown" and parsed["outcome_unknown"] is True
    assert "LEAK" not in str(parsed)
    assert set(parsed) == set(contract.result("run"))


@pytest.mark.parametrize("output", [None, "", "SUCCESS: Status: DONE", "[]", "null", '{"status":"passed"}'])
def test_invalid_transport_cannot_prove_success(output):
    assert contract.parse(output, "run")["status"] == "unknown"
    assert contract.parse(output, "health")["status"] == "blocked"


@pytest.mark.parametrize("prefix", [
    '"status":"failed","session_status":"ERROR",',
    '"status":"passed",',
    '"error_code":NaN,',
    '"error_code":Infinity,',
    '"error_code":-Infinity,',
])
def test_raw_json_rejects_duplicates_and_nonfinite_constants(prefix):
    raw = "{" + prefix + json.dumps(done())[1:]
    parsed = contract.parse(raw, "run")
    assert parsed["status"] == "unknown"
    assert parsed["error_code"] == "RemoteOutputInvalid"
    assert parsed["session_status"] is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_decoder_itself_rejects_nonfinite_before_schema_validation(monkeypatch, constant):
    decoder = json.loads
    rejected = []

    def observe_decode(value, **kwargs):
        try:
            return decoder(value, **kwargs)
        except ValueError:
            rejected.append(True)
            raise

    monkeypatch.setattr(contract.json, "loads", observe_decode)
    assert contract.parse('{"unexpected":' + constant + '}', "run")["status"] == "unknown"
    assert rejected == [True]


@pytest.mark.parametrize("approval", [False, None, "true", 1, [], {}])
def test_literal_approval_before_configuration_or_process(monkeypatch, approval):
    orch = oo.OdiOrchestrator()
    process = Mock(side_effect=AssertionError("process reached"))
    monkeypatch.setattr(orch, "run_groovy", process)
    monkeypatch.setattr(orch, "_remote_configuration_ready", lambda: pytest.fail("configuration reached"))
    assert orch.remote_agent_health(approved=approval)["error_code"] == "ApprovalRequired"
    assert orch.run_scenario_remote(approved=approval, mapping_name="MAP_TEST", scenario_name="MAP_TEST_SCN",
                                    version="001")["error_code"] == "ApprovalRequired"
    process.assert_not_called()


@pytest.mark.parametrize("url", ["", "http://host:15102/oraclediagent", "http://host:15101/OracleDIAgent",
                                  "http://host:15101/oraclediagent/other", "http://host:15101/oraclediagent?q=x",
                                  "http://u:p@host:15101/oraclediagent", "http://host:15101/oraclediagent#x",
                                  "file://host:15101/oraclediagent"])
def test_fixed_endpoint_guard_before_process(monkeypatch, url):
    monkeypatch.setattr(oo, "ODI_REST_URL", url)
    run = Mock(side_effect=AssertionError("process reached"))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    assert oo.OdiOrchestrator().remote_agent_health(approved=True)["error_code"] == "AgentConfigurationUnavailable"
    run.assert_not_called()


def mock_process(monkeypatch, *, stdout=None, stderr="", timeout=False):
    monkeypatch.setattr(oo, "ODI_REST_URL", "http://synthetic:15101/oraclediagent")
    monkeypatch.setattr(oo, "_ensure_local_props_file", lambda: "SYNTHETIC_UNUSED.properties")
    calls = []

    def process(command, **kwargs):
        calls.append(Path(command[-2]).name)
        if timeout:
            raise subprocess.TimeoutExpired(command, 300)
        return subprocess.CompletedProcess(command, 0, stdout=stdout or json.dumps(done()), stderr=stderr)

    monkeypatch.setattr(oo.subprocess, "run", process)
    return calls


def test_remote_only_legacy_adapter(monkeypatch):
    calls = mock_process(monkeypatch)
    with oo.groovy_approval_context(True):
        result = oo.OdiOrchestrator().run_scenario("MAP_TEST_SCN", "001", "GLOBAL")
    assert result == "SUCCESS: Remote scenario completed. Status: DONE"
    assert calls == ["run_scenario_remote.groovy"]


def test_legacy_script_cannot_bypass_remote_adapter(monkeypatch):
    process = Mock(side_effect=AssertionError("legacy process reached"))
    monkeypatch.setattr(oo.subprocess, "run", process)
    with oo.groovy_approval_context(True):
        result = oo.OdiOrchestrator().run_groovy("run_scenario.groovy", {})
    assert "LegacyRuntimeDisabled" in result
    process.assert_not_called()


def test_timeout_is_unknown_no_retry_no_raw_exception(monkeypatch):
    calls = mock_process(monkeypatch, timeout=True)
    result = oo.OdiOrchestrator().run_scenario_remote(approved=True, mapping_name="MAP_TEST",
                                                    scenario_name="MAP_TEST_SCN", version="001")
    assert result["status"] == "unknown" and result["error_code"] == "RemoteTimeout"
    assert result["run_may_have_started"] is True
    assert calls == ["run_scenario_remote.groovy"]
    assert "command" not in str(result)


def test_products_real_remote_path_preserves_timeout_unknown(monkeypatch):
    calls = mock_process(monkeypatch, timeout=True)
    result = oo.OdiOrchestrator().run_products_ods_scenario_safe(
        approved=True, approval_scope="odi68_products_ods_scenario_run_and_rerun_proof",
        scenario_name="MAP_SH_PRODUCTS_TO_ODS_SCN", version="001", context="GLOBAL",
    )
    assert calls == ["run_scenario_remote.groovy"]
    assert result["status"] == "unknown"
    assert result["outcome_unknown"] is True
    assert result["error_code"] == "RemoteTimeout"
    assert result["run_may_have_started"] is True
    assert result["side_effects"] is True
    assert result["raw_output_included"] is False and result["session_id_included"] is False
    assert "command" not in str(result) and "SYNTHETIC_UNUSED" not in str(result)


def test_fatal_stderr_cannot_be_overridden_by_done_json(monkeypatch):
    mock_process(monkeypatch, stderr="SEVERE: LEAK")
    result = oo.OdiOrchestrator().run_scenario_remote(approved=True, mapping_name="MAP_TEST",
                                                    scenario_name="MAP_TEST_SCN", version="001")
    assert result["status"] == "unknown" and "LEAK" not in str(result)


def setup_pipeline(monkeypatch, *, health=None, run=None, generation=None, option="SUCCESS: options updated"):
    events = []
    monkeypatch.setattr(oo.OdiOrchestrator, "remote_agent_health",
                        lambda *a, **k: events.append("health") or (health if health is not None else healthy()))
    monkeypatch.setattr(oo.OdiOrchestrator, "update_mapping", lambda *a, **k: events.append("options") or option)
    monkeypatch.setattr(oo.OdiOrchestrator, "generate_scenario_metadata",
                        lambda *a, **k: events.append("generate") or (generation if generation is not None else generated()))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_scenario_remote",
                        lambda *a, **k: events.append("run") or (run if run is not None else done()))
    monkeypatch.setattr(service, "_persist_run_state", lambda *a, **k: events.append("persist"))
    monkeypatch.setattr(service, "_create_failure_alert", lambda *a, **k: events.append("alert"))
    return events


@pytest.mark.parametrize("health", [contract.result("health", error_code="AgentConfigurationUnavailable"),
                                     contract.result("health", stage="health", error_code="AgentUnavailable", agent=True),
                                     {"status": "passed"}])
def test_health_failure_precedes_all_persistent_actions(monkeypatch, health):
    events = setup_pipeline(monkeypatch, health=health)
    outcome = service.execute_mapping_run(Mock(), service.MappingRunOptions("MAP_TEST", "001", "GLOBAL", True))
    assert outcome.status_code == 503 and events == ["health"]


def test_classic_order_and_status_only_result(monkeypatch):
    events = setup_pipeline(monkeypatch)
    outcome = service.execute_mapping_run(Mock(), service.MappingRunOptions("MAP_TEST", "001", "GLOBAL", False))
    assert outcome.status_code == 200
    assert events == ["health", "options", "generate", "run", "persist"]
    assert not {"log", "generation_log", "option_log", "sql", "session_id"} & set(outcome.payload)


@pytest.mark.parametrize("generation", [{"status": "passed"}, {**generated(), "status": "attention_required"},
                                          {**generated(), "scenario": {"name": "OTHER", "version": "001"}},
                                          {**generated(), "checks": [{"status": "fail"}]}])
def test_generation_failure_stops_before_run(monkeypatch, generation):
    events = setup_pipeline(monkeypatch, generation=generation)
    outcome = service.execute_mapping_run(Mock(), service.MappingRunOptions("MAP_TEST", "001", "GLOBAL", False))
    assert outcome.status_code == 400 and "run" not in events


@pytest.mark.parametrize("run", [contract.transport_failure("run", "RemoteTimeout"),
                                  contract.result("run", status="failed", stage="run", error_code="ScenarioRunFailed",
                                                  agent=True, repository=True, may_have_started=True, session_status="ERROR"),
                                  {"status": "passed"}])
def test_classic_remote_failures_are_explicit(monkeypatch, run):
    events = setup_pipeline(monkeypatch, run=run)
    outcome = service.execute_mapping_run(Mock(), service.MappingRunOptions("MAP_TEST", "001", "GLOBAL", False))
    assert outcome.status_code in {502, 504} and outcome.payload["status"] != "SUCCESS"
    assert events.count("run") == 1


def test_scd2_health_failure_precedes_catalog_and_run():
    orch = Mock()
    orch.remote_agent_health.return_value = contract.result("health", error_code="AgentConfigurationUnavailable")
    services = Mock(orchestrator_factory=lambda: orch)
    services.catalog_connection.side_effect = AssertionError("catalog reached")
    payload, code = scd2_service.run(REQUEST, services, approved=True)
    assert code == 503 and payload["status"] == "blocked_agent_preflight"
    services.catalog_connection.assert_not_called()
    orch.run_scenario_remote.assert_not_called()


@pytest.mark.parametrize("run", [done(), contract.transport_failure("run", "RemoteTimeout"), {"status": "passed"}])
def test_scd2_consumes_the_same_strict_remote_contract(run):
    orch = Mock()
    orch.run_scenario_remote.return_value = run
    step = scd2_odi.run_scenario(orch, "MAP_TEST_SCN", "001", "GLOBAL", True)
    assert step["status"] == ("passed" if run == done() else "unknown")
    assert orch.run_scenario_remote.call_args.kwargs["mapping_name"] == "MAP_TEST"
    orch.run_scenario_safe.assert_not_called()


def test_scd2_preserves_count_postflight_after_unknown(monkeypatch):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    orch = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(
        plan, mapping_exists=True, scenario_exists=True, target_datastore_exists=True)})
    orch.remote_agent_health = lambda **kw: healthy()
    orch.run_scenario_remote = lambda **kw: contract.transport_failure("run", "RemoteTimeout")
    monkeypatch.setattr(scd2_service, "_target_is_scd2", lambda *a: True)
    count_calls = []
    monkeypatch.setattr(scd2_service.scd2_postflight, "collect_stock_postflight",
                        lambda *a, **k: count_calls.append(1) or {"status": "ok", "stock_contract_valid": True,
                            "source_current_count_equal": True, "counts": {"target_total": 3}})
    payload, code = scd2_service.run(REQUEST, make_services(orch), approved=True)
    assert code == 502 and payload["status"] == "run_unknown" and len(count_calls) == 2


def test_http_run_adapters_keep_approval_and_rate_without_legacy_process():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "routes" / "misc_write.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"api_airflow_run_odi", "api_airflow_run_mapping"}:
            source = ast.unparse(node)
            assert "_approval_error" in source and "5 per minute" in source
            assert "run_scenario_remote" in source and "full_odi_run.py" not in source
            assert "subprocess.run" not in source and "csrf.exempt" not in source


def isolated_http_handler(filename, function, body):
    """Load just the actual handler and approval helper, never app bootstrap."""
    import re

    from flask import jsonify, request

    root = Path(__file__).resolve().parents[1]
    approval_tree = ast.parse((root / "app.py").read_text(encoding="utf-8-sig"))
    approval = next(n for n in approval_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_approval_error")
    tree = ast.parse((root / "routes" / filename).read_text(encoding="utf-8"))
    handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    handler.decorator_list = []
    namespace = {"jsonify": jsonify, "request": request, "groovy_approval_context": oo.groovy_approval_context,
                 "execute_mapping_run": service.execute_mapping_run,
                 "validate_mapping_run_options": service.validate_mapping_run_options}
    compiled = compile(ast.Module(body=[approval, handler], type_ignores=[]), filename, "exec")
    for code in compiled.co_consts:
        if isinstance(code, CodeType) and code.co_name in {"_approval_error", function}:
            namespace[code.co_name] = FunctionType(code, namespace)
    app = SimpleNamespace(_approval_error=namespace["_approval_error"],
                          _valid_identifier=lambda x: re.fullmatch(r"[A-Z][A-Z0-9_]*", x),
                          _valid_version=lambda x: re.fullmatch(r"[A-Za-z0-9_.-]+", x),
                          subprocess=subprocess)
    namespace["get_app_module"] = lambda: app
    namespace["redacted_write_error"] = lambda *a: pytest.fail("unexpected handler exception")
    return namespace[function]


@pytest.mark.parametrize("filename,function,body", [
    ("mapping_write.py", "api_mapping_run", {"mapping_name": "MAP_TEST", "truncate_target_table": False}),
    ("misc_write.py", "api_airflow_run_odi", {"scen_name": "MAP_TEST_SCN"}),
    ("misc_write.py", "api_airflow_run_mapping", {"mapping_name": "MAP_TEST"}),
])
@pytest.mark.parametrize("approval", [False, None, "true", 1])
def test_actual_http_handlers_reject_nonliteral_approval_before_adapter(monkeypatch, filename, function, body, approval):
    from flask import Flask

    factory = Mock(side_effect=AssertionError("orchestrator reached"))
    monkeypatch.setattr(oo, "OdiOrchestrator", factory)
    handler = isolated_http_handler(filename, function, body)
    with Flask(__name__).test_request_context(json={**body, "approved": approval}):
        response, status = handler()
    assert status == 400 and "approved:true" in response.get_json()["error"]
    factory.assert_not_called()


@pytest.mark.parametrize("function,health_failure", [("api_airflow_run_odi", False),
                                                     ("api_airflow_run_mapping", False),
                                                     ("api_airflow_run_mapping", True)])
def test_actual_legacy_http_handlers_use_remote_only(monkeypatch, function, health_failure):
    from flask import Flask

    orch = Mock()
    orch.remote_agent_health.return_value = (contract.result("health", error_code="AgentUnavailable")
                                             if health_failure else healthy())
    orch.generate_scenario_metadata.return_value = generated()
    orch.run_scenario_remote.return_value = done()
    monkeypatch.setattr(oo, "OdiOrchestrator", lambda: orch)
    body = {"approved": True, "mapping_name": "MAP_TEST", "scen_name": "MAP_TEST_SCN"}
    handler = isolated_http_handler("misc_write.py", function, body)
    with Flask(__name__).test_request_context(json=body):
        response, code = handler()
    if health_failure:
        assert code == 503
        orch.generate_scenario_metadata.assert_not_called()
        orch.run_scenario_remote.assert_not_called()
    else:
        assert code == 200 and response.get_json() == done()
        orch.run_scenario_remote.assert_called_once()
    orch.run_scenario.assert_not_called()


@pytest.mark.parametrize("method,filename,function,path", [
    ("test_mapping_run_operator_failure_response_is_redacted", "mapping_write.py",
     "api_mapping_run", "/api/mapping/run"),
    ("test_legacy_airflow_mapping_operator_failure_response_is_redacted", "misc_write.py",
     "api_airflow_run_mapping", "/api/airflow/run-mapping"),
])
def test_reported_redaction_regressions_with_isolated_real_handlers(monkeypatch, method, filename, function, path):
    import sys
    from unittest.mock import MagicMock

    from flask import Flask

    from tests.test_api_endpoints import TestSecondaryWriteErrorRedaction

    handler = isolated_http_handler(filename, function, {})
    app_module = handler.__globals__["get_app_module"]()
    app_module.MappingStateManager = Mock()
    app_module.AlertManager = MagicMock()
    app_module.logger = Mock()
    # No masking here: the original assertions must catch any leaked operator text.
    app_module.redact_sensitive_text = lambda value, **kwargs: str(value)
    monkeypatch.setitem(sys.modules, "app", app_module)
    app = Flask(__name__)
    app.add_url_rule(path, view_func=handler, methods=["POST"])
    with app.test_client() as client:
        getattr(TestSecondaryWriteErrorRedaction(), method)(client, monkeypatch)
