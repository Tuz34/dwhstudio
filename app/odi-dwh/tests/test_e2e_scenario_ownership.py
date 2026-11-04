"""Exact-version reuse with SDK objects, fake finders and no repository connection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import mapping_run_service
import odi_orchestrator
from mapping_run_service import MappingRunOptions, execute_mapping_run
from odi_orchestrator import OdiOrchestrator, groovy_approval_context

SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation" / "generate_scenario_metadata.groovy"

# Only lookup helpers and the pre-transaction reuse decision are evaluated.
# Neither the connection/bootstrap block nor the generation body is executed.
SCENARIO_HARNESS = r'''
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.Tag
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder

String source = new File(args[0]).text.replace('\r\n', '\n')
String helpers = source.substring(source.indexOf('def scenariosByName ='),
    source.indexOf('try {\n    try {'))
String decision = source.substring(source.indexOf('    IOdiScenarioFinder scenFinder ='),
    source.indexOf('    ITransactionManager txnMgr ='))
assert !decision.contains('ScenarioUtils') && !decision.contains('getTransaction(')
String imports = 'import groovy.json.JsonOutput\n' +
    'import oracle.odi.domain.runtime.scenario.OdiScenario\n' +
    'import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder\n'
def cases = new JsonSlurper().parseText(args[1])
def results = cases.collect { spec ->
    def scenarios = spec.scenarios.collect { row ->
        def scenario = new OdiScenario(new Tag(row.name ?: 'MAP_TEST_SCN', row.version))
        scenario.setSourceMappingId(row.owner)
        scenario
    }
    def lookups = []
    def finder = [
        findByName: { String name ->
            lookups << 'by_name'
            if (spec.fallback) throw new UnsupportedOperationException('synthetic finder')
            scenarios
        },
        findAll: { -> lookups << 'all'; scenarios },
        findLatestByName: { String name ->
            lookups << 'latest'
            scenarios.findAll { it.name == name }.max { it.version }
        },
    ] as IOdiScenarioFinder
    def checks = []
    def addCheck = { String name, String status, String detail, Map metadata = [:] ->
        checks << [name: name, status: status, detail: detail, metadata: metadata]
    }
    def output = new StringWriter()
    def binding = new Binding([
        mapping: new Expando(getInternalId: { -> spec.mapping }),
        em: new Expando(getFinder: { type -> assert type == OdiScenario; finder }),
        scenarioName: 'MAP_TEST_SCN', scenarioVersion: spec.requested,
        checks: checks, addCheck: addCheck, out: new PrintWriter(output),
    ])
    def result = new GroovyShell(this.class.classLoader, binding).evaluate(
        imports + helpers + decision + '\nreturn [status: "generation_required"]')
    [payload: output.toString().trim() ? new JsonSlurper().parseText(output.toString()) : result,
     lookups: lookups]
}
println JsonOutput.toJson(results)
'''


def _case(owner, *, mapping=42, requested="001", fallback=False, latest_owner=42):
    return {
        "mapping": mapping, "requested": requested, "fallback": fallback,
        "scenarios": [{"version": requested, "owner": owner},
                      {"version": "002" if requested == "001" else "008", "owner": latest_owner}],
    }


CASES = [
    _case(99),
    _case(None),
    _case(42, mapping=None),
    _case(99, requested="007", fallback=True),
    _case(None, fallback=True),
    _case(42),
    _case(42, latest_owner=99),
    _case(42, fallback=True),
    {"mapping": 42, "requested": "001", "scenarios": [{"version": "002", "owner": 42}]},
    {"mapping": 42, "requested": "001", "scenarios": [{"name": "OTHER_SCN", "version": "001", "owner": 42}]},
    {"mapping": 42, "requested": "001", "fallback": True,
     "scenarios": [{"name": "OTHER_SCN", "version": "001", "owner": 42}]},
]


@pytest.fixture(scope="module")
def scenario_results():
    required = [Path(odi_orchestrator._JAVA_BIN), Path(odi_orchestrator._GROOVY_JAR),
                Path(odi_orchestrator._ODI_SDK_LIB) / "odi-core.jar"]
    if not all(path.is_file() for path in required):
        pytest.skip("Local Java/ODI SDK/Groovy unavailable; nothing is downloaded.")
    result = subprocess.run(
        [str(required[0]), "-cp", OdiOrchestrator()._build_classpath(),
         "groovy.ui.GroovyMain", "-e", SCENARIO_HARNESS, str(SCRIPT), json.dumps(CASES)],
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("index", range(5))
def test_exact_version_missing_or_wrong_owner_blocks_reuse(scenario_results, index):
    payload = scenario_results[index]["payload"]
    assert payload["status"] == "attention_required"
    assert payload["side_effects"] is False
    assert any(check["name"] == "scenario_exact_mapping_owner" and check["status"] == "fail"
               for check in payload["checks"])
    assert not any(check["name"] == "scenario_already_ready" for check in payload["checks"])


@pytest.mark.parametrize("index", [5, 6, 7])
def test_matching_older_owner_is_reused_without_generation(scenario_results, index):
    payload = scenario_results[index]["payload"]
    assert payload["status"] == "already_ready"
    assert payload["side_effects"] is False
    assert payload["scenario"]["version"] == "001"
    assert payload["scenario"]["latest_version"] == "002"


@pytest.mark.parametrize("index", [8, 9, 10])
def test_absent_exact_name_version_does_not_reuse_other_scenarios(scenario_results, index):
    assert scenario_results[index]["payload"]["status"] == "generation_required"


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4, 5])
def test_actual_generator_decision_controls_real_run_pipeline(scenario_results, monkeypatch, index):
    from tests.test_remote_run_contract import healthy, done

    calls = []
    payload = scenario_results[index]["payload"]

    def process_result(command, **kwargs):
        script = Path(command[-2]).name
        calls.append(script)
        assert script in {"generate_scenario_metadata.groovy", "run_scenario_remote.groovy"}
        stdout = json.dumps(payload) if script == "generate_scenario_metadata.groovy" else json.dumps(done())
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(odi_orchestrator, "ODI_REST_URL", "http://synthetic:15101/oraclediagent")
    monkeypatch.setattr(OdiOrchestrator, "remote_agent_health", lambda *args, **kwargs: healthy())
    monkeypatch.setattr(OdiOrchestrator, "update_mapping", lambda *args, **kwargs: "SUCCESS: options updated")
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: "SYNTHETIC_UNUSED.properties")
    monkeypatch.setattr(odi_orchestrator.subprocess, "run", process_result)
    persist = Mock()
    monkeypatch.setattr(mapping_run_service, "_persist_run_state", persist)
    monkeypatch.setattr(mapping_run_service, "_create_failure_alert", Mock())
    app = Mock()
    app.redact_sensitive_text.side_effect = lambda text, **kwargs: text
    with groovy_approval_context(True):
        outcome = execute_mapping_run(app, MappingRunOptions("MAP_TEST", CASES[index]["requested"], "GLOBAL", False))

    if index == 5:
        assert outcome.status_code == 200
        assert calls == ["generate_scenario_metadata.groovy", "run_scenario_remote.groovy"]
    else:
        assert calls == ["generate_scenario_metadata.groovy"]
        assert outcome.status_code == 400
        assert persist.call_args.args[4:7] == ("generate_scenario", "RUN_FAILED", ["update_km"])
