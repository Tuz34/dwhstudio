"""Compile and execute extracted pure closures with fake invokers only."""

import os
import subprocess
from pathlib import Path

import pytest

import odi_orchestrator as oo
from tests.test_e2e_generic_groovy import test_changed_scripts_parse as parse_script
from tests.test_e2e_generic_groovy import test_changed_scripts_sdk_compile_only as compile_script

ROOT = Path(__file__).resolve().parents[1] / "odi_automation"

REMOTE_HARNESS = r'''
import oracle.odi.runtime.agent.invocation.StartupParams
String source = new File(args[0]).text.replace('\r\n', '\n')
String helpers = source.substring(source.indexOf('def validateAgent ='), source.indexOf('Map payload ='))
def program = helpers + """
def props = new Properties()
props.setProperty('agent_url', 'http://synthetic:15101/oraclediagent')
props.setProperty('agent_username', 'fixture')
props.setProperty('agent_password', 'fixture')
props.setProperty('work_name', 'fixture')
def request = [name: 'MAP_TEST_SCN', version: '001', context: 'GLOBAL']
for (spec in ['DONE', 'ERROR', 'RUNNING', 'null_info', 'null_status', 'throw', 'health_fail', 'owner_fail', 'cleanup_fail']) {
    def events = []
    def invoker = new Expando(
        invokeIsAlive: { -> events << 'health'; if (spec == 'health_fail') throw new RuntimeException('LEAK'); '12' },
        invokeStartScenario: { name, version, startup, keywords, context, level, session, synchronous, work ->
            events << 'run'
            assert name == 'MAP_TEST_SCN' && version == '001' && context == 'GLOBAL'
            assert startup instanceof StartupParams && synchronous == true && work == 'fixture'
            if (spec == 'throw') throw new RuntimeException('LEAK')
            spec == 'null_info' ? null : new Expando(getSessionStatus: { spec == 'null_status' ? null : spec })
        })
    def payload = [status: 'blocked', stage: 'configuration', error_code: 'AgentConfigurationUnavailable',
        agent_contact_attempted: false, repository_connection_attempted: false, run_may_have_started: false,
        side_effects: false, outcome_unknown: false, session_status: null]
    executeRemote(payload, request, props, { invoker }, { r, p ->
        events << 'owner'
        if (spec == 'owner_fail') throw new RuntimeException('LEAK')
        if (spec == 'cleanup_fail') { payload.error_code = 'RepositoryCleanupFailed'; throw new RuntimeException('LEAK') }
    })
    if (spec in ['health_fail', 'owner_fail', 'cleanup_fail']) {
        assert !events.contains('run') && !payload.side_effects && payload.status == 'blocked'
    } else {
        assert events == ['health', 'owner', 'run']
        assert payload.status == (spec == 'DONE' ? 'passed' : spec == 'ERROR' ? 'failed' : 'unknown')
        assert payload.outcome_unknown == !(spec in ['DONE', 'ERROR'])
    }
    assert !payload.toString().contains('LEAK')
}
for (spec in [[42,42,'001','MAP_TEST_SCN',true], [42,99,'001','MAP_TEST_SCN',false],
              [42,null,'001','MAP_TEST_SCN',false], [null,42,'001','MAP_TEST_SCN',false],
              [42,42,'002','MAP_TEST_SCN',false], [42,42,'001','OTHER',false]]) {
    boolean passed = false
    try {
        requireOwnership(new Expando(getInternalId: { spec[0] }),
            new Expando(getSourceMappingId: { spec[1] }, getVersion: { spec[2] }, getName: { spec[3] }),
            'MAP_TEST_SCN', '001')
        passed = true
    } catch (IllegalStateException ignored) {}
    assert passed == spec[4]
}
for (url in ['', 'http://synthetic:15102/oraclediagent', 'http://synthetic:15101/OracleDIAgent',
             'http://synthetic:15101/oraclediagent?q=x', 'http://u:p@synthetic:15101/oraclediagent']) {
    props.setProperty('agent_url', url)
    boolean blocked = false
    try { validateAgent(props) } catch (IllegalArgumentException ignored) { blocked = true }
    assert blocked
}
println 'REMOTE_CONTRACT_OK'
"""
new GroovyShell(this.class.classLoader).evaluate('import oracle.odi.runtime.agent.invocation.StartupParams\n' + program)
'''


@pytest.mark.parametrize("name", ["remote_agent_health.groovy", "run_scenario_remote.groovy"])
def test_remote_compile_only(name):
    parse_script(name)
    compile_script(name)


def test_pure_remote_protocol_with_fake_invoker():
    required = (Path(oo._JAVA_BIN), Path(oo._GROOVY_JAR), Path(oo._ODI_SDK_LIB) / "odi-core.jar")
    if not all(path.is_file() for path in required):
        pytest.skip("Local Java/Groovy/ODI SDK runtime unavailable.")
    # The production builder currently emits Windows separators; Java needs the host separator.
    entries = oo.OdiOrchestrator()._build_classpath().split(";")
    if not all(Path(entry).parent.is_dir() if Path(entry).name == "*" else Path(entry).is_file()
               for entry in entries):
        pytest.skip("Local ODI SDK classpath dependencies unavailable.")
    classpath = os.pathsep.join(entries)
    result = subprocess.run([oo._JAVA_BIN, "-cp", classpath,
                             "groovy.ui.GroovyMain", "-e", REMOTE_HARNESS,
                             str(ROOT / "run_scenario_remote.groovy")],
                            capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REMOTE_CONTRACT_OK" in result.stdout


def test_config_guards_identical_and_no_legacy_or_raw_output():
    health = (ROOT / "remote_agent_health.groovy").read_text(encoding="utf-8")
    runner = (ROOT / "run_scenario_remote.groovy").read_text(encoding="utf-8")
    assert health[health.index("def validateAgent ="):health.index("Map payload =")].strip() == runner[
        runner.index("def validateAgent ="):runner.index("def requireOwnership =")].strip()
    for text in (health, runner):
        for forbidden in ("getMessage(", "printStackTrace", "getSessionId", "Class.forName", ".commit("):
            assert forbidden not in text
    assert "invokeStartScenario" not in health and "OdiInstance" not in health
    assert "findByTag(new Tag(spec.name, spec.version))" in runner
    assert runner.index("checkOwnership(request, props)") < runner.index("invoker.invokeStartScenario")
