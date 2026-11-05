"""ODI-88: a changed mapping never commits when the final SDK validation reports errors.

The Groovy tests execute the real commit/rollback fragments of the production scripts against
in-memory fakes (Groovy only: no ODI SDK instance, repository, database or scenario). The fakes are
overload-agnostic: each validate() call, with or without an issue list, consumes the next scripted
result, so the tests fix the behaviour and not one particular SDK call shape.
SDK semantics, confirmed in the installed odi-core.jar bytecode and create_mapping.groovy:
true means errors were found, and Root.validate() returns validate(list, false).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import odi_orchestrator
from odi_orchestrator import OdiOrchestrator, groovy_approval_context

ROOT = Path(__file__).resolve().parents[1] / "odi_automation"
SECRET = "password=hunter2 SELECT PK_VALUE FROM T_SECRET id=991"
BUILT_IN = "LKM SQL to Oracle (Built-In)"
DB_LINK = "LKM Oracle to Oracle Pull (DB Link)"

FAKES = r'''
import groovy.json.JsonOutput
class MapPhysicalNode {
    String name; String lkmName; List events
    void setLKM(km) { lkmName = km.name; events << ("setLKM:" + name) }
    String getLKMName() { lkmName }
}
class FakeIssue {
    String message
    String toString() { message }
    String getMessage() { message }
    String getLevel() { "ERROR" }
}
class FakeMapping {
    List events; List results; List nodes; boolean materialize; boolean explode; int calls = 0
    private boolean next() {
        calls++
        events << "validate"
        if (explode) throw new RuntimeException(SECRET_TEXT)
        if (materialize && calls == 1) {
            nodes << new MapPhysicalNode(name: "LATE_AP", lkmName: DB_LINK_NAME, events: events)
        }
        return results[Math.min(calls - 1, results.size() - 1)]
    }
    boolean validate() { next() }
    boolean validate(List issues, boolean flag) {
        boolean result = next()
        if (result) issues << new FakeIssue(message: SECRET_TEXT)
        return result
    }
    static String SECRET_TEXT
    static String DB_LINK_NAME
}
def events = []
FakeMapping.SECRET_TEXT = __SECRET__
FakeMapping.DB_LINK_NAME = __DB_LINK__
addShutdownHook { System.out.println("EVENTS:" + JsonOutput.toJson(events)); System.out.flush() }
def txnStatus = new Expando(completed: false)
txnStatus.isCompleted = { txnStatus.completed }
def txnMgr = new Expando(
    commit: { status -> events << "commit"; status.completed = true },
    rollback: { status -> events << "rollback"; status.completed = true })
'''


def _text(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _between(source, start, end):
    assert source.count(start) == 1, start
    return start + source.split(start, 1)[1].split(end, 1)[0]


def _groovy():
    java = shutil.which("java") or str(
        Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")) / "bin" / "java.exe"
    )
    home = Path(os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    jar = home / "oracle_common/modules/thirdparty" / os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    if not (Path(java).is_file() and jar.is_file()):
        pytest.skip("Java or the Groovy jar is unavailable; nothing is downloaded.")
    return [java, "-cp", str(jar), "groovy.ui.GroovyMain"]


def _run(tmp_path, script):
    prelude = FAKES.replace("__SECRET__", json.dumps(SECRET)).replace("__DB_LINK__", json.dumps(DB_LINK))
    path = tmp_path / "harness.groovy"
    path.write_text(prelude + script, encoding="utf-8")
    done = subprocess.run([*_groovy(), str(path)], capture_output=True, text=True, timeout=120, check=False)
    assert "EVENTS:" in done.stdout, done.stdout + done.stderr
    head, tail = done.stdout.split("EVENTS:", 1)
    return done.returncode, head, json.loads(tail.strip()), done.stderr


def _scd2(tmp_path, results, *, nodes="[]", materialize=False, explode=False):
    source = _text("scd2_mapping_create.groovy")
    header = _between(source, "def safeFlags = {", "def requireProp = {")
    body = _between(source, "    def readAccessPoints = ", "} finally {")
    setup = f'''
final String TOOL = "odi.scd2_mapping_create"
final String MODE = "generic_absent_only_scd2_mapping_creation"
final String TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
final String LKM_NAME = "{BUILT_IN}"
def lkm = [name: LKM_NAME]
def apNodes = {nodes}
def mapping = new FakeMapping(events: events, results: {json.dumps(results)}, nodes: apNodes,
    materialize: {str(materialize).lower()}, explode: {str(explode).lower()})
def design = new Expando(getAllAPNodes: {{ apNodes }})
def attribute = new Expando(getExpression: {{ "EXPR" }})
def targetComponent = new Expando(getAttributes: {{ [attribute] }})
def target = new Expando(getColumns: {{ ["COL"] }})
'''
    return _run(tmp_path, header + setup + "try {\n" + body + "}\n")


def _update(tmp_path, results, *, changed=True, explode=False):
    source = _text("update_mapping.groovy")
    classes = "\n".join(
        match.group(0) for match in re.finditer(r"^class \w+ extends .*?^}\n", source, re.M | re.S)
    )
    body = _between(source, "    if (changed) {\n", "} finally {")
    setup = f'''
boolean changed = {str(changed).lower()}
String mName = "M"
String operation = "update_km"
def mapping = new FakeMapping(events: events, results: {json.dumps(results)}, nodes: [], explode: {str(explode).lower()})
'''
    return _run(tmp_path, classes + setup + "try {\n" + body + "}\n")


def _payload(stdout):
    return json.loads(stdout.strip().splitlines()[-1])


def _assert_no_leak(*texts):
    for text in texts:
        for marker in ("hunter2", "T_SECRET", "PK_VALUE", "991"):
            assert marker not in text


# --- scd2_mapping_create.groovy ---------------------------------------------------------------


def test_scd2_final_validation_errors_never_reach_commit(tmp_path):
    code, out, events, err = _scd2(tmp_path, [False, True])
    assert "commit" not in events
    assert events[-1] == "rollback"
    payload = _payload(out)
    assert payload["status"] == "error"
    assert payload["side_effects"] is False
    assert payload["error_type"] == "MappingValidationFailed"
    assert "validation_messages" not in payload
    _assert_no_leak(out, err)


def test_scd2_clean_final_validation_commits(tmp_path):
    _, out, events, _ = _scd2(tmp_path, [False, False])
    assert events.count("commit") == 1
    assert "rollback" not in events
    assert _payload(out)["status"] == "passed"


def test_scd2_final_validation_runs_after_repair_and_before_commit(tmp_path):
    _, _, events, _ = _scd2(tmp_path, [False, False])
    assert events.index("validate") < len(events) - 1
    assert events[-2:] == ["validate", "commit"]


def test_scd2_early_error_on_repairable_state_does_not_reject_the_mapping(tmp_path):
    """The first validate materializes access points; only the final result decides."""
    _, out, events, _ = _scd2(tmp_path, [True, False], materialize=True)
    payload = _payload(out)
    assert payload["status"] == "passed"
    assert payload["result"]["access_point_count"] == 1
    assert payload["result"]["lkm_assigned_count"] == 1
    assert events == ["validate", "setLKM:LATE_AP", "validate", "commit"]


def test_scd2_early_and_final_errors_reject(tmp_path):
    _, out, events, _ = _scd2(tmp_path, [True, True], materialize=True)
    assert "commit" not in events
    assert _payload(out)["error_type"] == "MappingValidationFailed"


def test_scd2_validate_exception_rolls_back_with_class_name_only(tmp_path):
    _, out, events, err = _scd2(tmp_path, [False], explode=True)
    assert "commit" not in events and events[-1] == "rollback"
    assert _payload(out)["error_type"] == "RuntimeException"
    _assert_no_leak(out, err)


def test_scd2_lkm_guard_still_precedes_the_final_validation(tmp_path):
    nodes = f'[new MapPhysicalNode(name: "AP", lkmName: "{DB_LINK}", events: events)]'
    _, out, events, _ = _scd2(tmp_path, [False, False], nodes=nodes)
    assert _payload(out)["status"] == "passed"
    assert events[:2] == ["setLKM:AP", "validate"]


# --- update_mapping.groovy --------------------------------------------------------------------


def test_update_validation_errors_never_reach_commit(tmp_path):
    code, out, events, err = _update(tmp_path, [True])
    assert code == 1
    assert "commit" not in events
    assert events == ["validate", "rollback"]
    assert _payload(out) == {
        "status": "error",
        "action": "update_mapping",
        "error_type": "MappingValidationFailed",
    }
    assert "SUCCESS" not in out
    _assert_no_leak(out, err)


def test_update_clean_validation_commits(tmp_path):
    code, out, events, _ = _update(tmp_path, [False])
    assert code == 0
    assert events == ["validate", "commit"]
    assert "SUCCESS: Mapping 'M' updated" in out


def test_update_validate_exception_rolls_back_with_class_name_only(tmp_path):
    code, out, events, err = _update(tmp_path, [False], explode=True)
    assert code == 1
    assert "commit" not in events and events[-1] == "rollback"
    assert _payload(out)["error_type"] == "RuntimeException"
    _assert_no_leak(out, err)


def test_update_no_op_keeps_its_behaviour_without_validation(tmp_path):
    code, out, events, _ = _update(tmp_path, [True], changed=False)
    assert code == 0
    assert events == ["commit"]
    assert "No changes needed" in out


def test_update_validation_failure_stays_generic_through_run_groovy(monkeypatch):
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(OdiOrchestrator, "_build_classpath", lambda _self: "cp")
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: "local.properties")
    line = '{"status":"error","action":"update_mapping","error_type":"MappingValidationFailed"}\n'
    monkeypatch.setattr(
        odi_orchestrator.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, f"progress {SECRET}\n{line}", SECRET),
    )
    with groovy_approval_context(True):
        output = OdiOrchestrator().run_groovy("update_mapping.groovy", {})
    assert output.startswith("Hata:")
    _assert_no_leak(output)


# --- static guard for CI, where Groovy is absent and the behaviour tests above skip ------------


@pytest.mark.parametrize("name", ["scd2_mapping_create.groovy", "update_mapping.groovy"])
def test_final_gate_directly_precedes_the_changed_mapping_commit(name):
    source = _text(name)
    gate = source.rindex("mapping.validate(new ArrayList(), false)")
    assert "MappingValidationFailed" in source[gate:source.index("txnMgr.commit(txnStatus)", gate)]
    assert source.rfind("mapping.validate()") < gate
