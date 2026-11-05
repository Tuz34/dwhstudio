"""ODI-88: a changed mapping never commits when the final SDK validation reports errors.

The Groovy tests execute the real commit/rollback fragments of the production scripts on the
installed SDK classpath, with no OdiInstance, repository, database or scenario.

What is real and what is stubbed:
- Real: the production fragments and oracle.odi.domain.root.RootIssue with isErrorOrFatal.
- Stubbed: the Mapping (FakeMapping scripts each validate result and the issues it adds), the access
  point nodes, and the transaction manager. The stub cannot prove what the SDK Mapping does at run
  time, and nothing here proves a real mapping validates clean.

Each validate() call, with or without an issue list, consumes the next scripted result.

SDK semantics, from javap on the installed odi-core.jar and create_mapping.groovy:
- true means errors were found, and Root.validate() returns validate(list, false).
- Mapping.validate(List, boolean) can return false after adding a FATAL RootIssue: a non-overridable
  target load order error adds the issue at offsets 143-164 and offsets 170-172 return the earlier
  boolean unchanged. The gate therefore needs both the returned boolean and the issue list.
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
import oracle.odi.domain.root.RootIssue
class MapPhysicalNode {
    String name; String lkmName; List events
    Map options=[:]
    void setLKM(km) { lkmName = km.name; events << ("setLKM:" + name) }
    String getLKMName() { lkmName }
    String getTargetComponentKMName() { 'IKM Oracle Slowly Changing Dimension' }
    def getIKMOptionValue(String n) { options[n] }
}
class FakeMapping {
    List events; List results; List nodes; boolean materialize; boolean explode; int calls = 0
    String name
    List issuesByCall = []
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
        // Issues scripted for the call next() just consumed; the no-arg overload has no list to fill.
        List levels = calls <= issuesByCall.size() ? issuesByCall[calls - 1] : []
        levels.each { issues << new RootIssue(RootIssue.Level.valueOf(it), SECRET_TEXT) }
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
    sdk = home / "odi/sdk/lib"
    if not (Path(java).is_file() and jar.is_file() and (sdk / "odi-core.jar").is_file()):
        pytest.skip("Java, the Groovy jar or the ODI SDK is unavailable; nothing is downloaded.")
    return [java, "-cp", os.pathsep.join([str(sdk / "*"), str(jar)]), "groovy.ui.GroovyMain"]


def _run(tmp_path, script):
    prelude = FAKES.replace("__SECRET__", json.dumps(SECRET)).replace("__DB_LINK__", json.dumps(DB_LINK))
    path = tmp_path / "harness.groovy"
    path.write_text(prelude + script, encoding="utf-8")
    done = subprocess.run([*_groovy(), str(path)], capture_output=True, text=True, timeout=120, check=False)
    assert "EVENTS:" in done.stdout, done.stdout + done.stderr
    head, tail = done.stdout.split("EVENTS:", 1)
    return done.returncode, head, json.loads(tail.strip()), done.stderr


def _scd2(tmp_path, results, *, nodes="[]", materialize=False, explode=False, issues=()):
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
    materialize: {str(materialize).lower()}, explode: {str(explode).lower()},
    issuesByCall: {json.dumps(list(issues))})
def design = new Expando(getAllAPNodes: {{ apNodes }})
def attribute = new Expando(getExpression: {{ "EXPR" }})
def targetComponent = new Expando(getAttributes: {{ [attribute] }})
def target = new Expando(getColumns: {{ ["COL"] }})
boolean commitAttempted = false
boolean commitAcknowledged = false
def lockedOptions = [FLOW_CONTROL:false, STATIC_CONTROL:false, RECYCLE_ERRORS:false,
    TRUNCATE:false, DELETE_ALL:false, CREATE_TARG_TABLE:false, COMMIT:true]
def optionValues = lockedOptions.collectEntries {{ n, expected ->
    def value = new Expando()
    value.setValue = {{ v -> value.current = v }}
    value.getOptionValue = {{ -> value.current }}
    value.current = expected
    [(n): value]
}}
def targetNode = new MapPhysicalNode(name:'TARGET', events:events, options:optionValues)
def findTargetNode = {{ -> targetNode }}
def writeOptions = {{ node -> lockedOptions.each {{ n, v -> node.getIKMOptionValue(n).setValue(v) }} }}
'''
    return _run(tmp_path, header + setup + "try {\n" + body + "}\n")


def _update(tmp_path, results, *, changed=True, explode=False, issues=()):
    source = _text("update_mapping.groovy")
    classes = "\n".join(
        match.group(0) for match in re.finditer(r"^class \w+ extends .*?^}\n", source, re.M | re.S)
    )
    body = _between(source, "    if (changed) {\n", "} finally {")
    setup = f'''
boolean changed = {str(changed).lower()}
String mName = "M"
String operation = "update_km"
def mapping = new FakeMapping(events: events, results: {json.dumps(results)}, nodes: [], explode: {str(explode).lower()},
    issuesByCall: {json.dumps(list(issues))})
'''
    return _run(tmp_path, classes + setup + "try {\n" + body + "}\n")


def _create(tmp_path, results, *, replacing=True, explode=False, issues=()):
    source = _text("create_mapping.groovy")
    helpers = _between(source, "def summarizeIssues = {", "def odi = null")
    body = _between(source, "    List validationIssues = new ArrayList()\n", "} finally {")
    setup = f'''
String mName = "M"
boolean preserveExistingOnFailure = {str(replacing).lower()}
def existingMapping = new Object()
List validationMessages = []
Map physicalDiag = [same_schema: false, access_points: 0]
def mapping = new FakeMapping(events: events, results: {json.dumps(results)}, nodes: [], explode: {str(explode).lower()},
    issuesByCall: {json.dumps(list(issues))})
def design = new Expando(validate: {{ list, only -> false }}, getPhysicalNodes: {{ [] }})
def em = new Expando(remove: {{ target -> events << "remove" }}, flush: {{ events << "flush" }})
'''
    return _run(tmp_path, helpers + setup + "try {\n" + body + "}\n")


def _payload(stdout):
    return json.loads(stdout.strip().splitlines()[-1])


def _assert_no_leak(*texts):
    for text in texts:
        for marker in ("hunter2", "T_SECRET", "PK_VALUE", "991"):
            assert marker not in text


# --- scd2_mapping_create.groovy ---------------------------------------------------------------


def test_scd2_final_validation_errors_never_reach_commit(tmp_path):
    code, out, events, err = _scd2(tmp_path, [False, True], issues=[[], ["ERROR"]])
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


@pytest.mark.parametrize("levels", [["FATAL"], ["ERROR"], ["WARNING", "FATAL"], ["INFO", "ERROR"]])
def test_scd2_false_with_error_or_fatal_issue_never_reaches_commit(tmp_path, levels):
    """Mapping.validate can return false after adding a FATAL issue; the list must also block."""
    _, out, events, err = _scd2(tmp_path, [False, False], issues=[[], levels])
    assert "commit" not in events
    assert events[-1] == "rollback"
    payload = _payload(out)
    assert payload["status"] == "error"
    assert payload["error_type"] == "MappingValidationFailed"
    assert "validation_messages" not in payload
    _assert_no_leak(out, err)


@pytest.mark.parametrize("levels", [[], ["WARNING"], ["INFO"], ["WARNING", "INFO"]])
def test_scd2_false_with_only_lower_level_issues_commits(tmp_path, levels):
    _, out, events, _ = _scd2(tmp_path, [False, False], issues=[[], levels])
    assert events.count("commit") == 1
    assert "rollback" not in events
    assert _payload(out)["status"] == "passed"


@pytest.mark.parametrize("levels", [[], ["WARNING"]])
def test_scd2_true_without_error_issues_is_still_rejected(tmp_path, levels):
    _, out, events, _ = _scd2(tmp_path, [False, True], issues=[[], levels])
    assert "commit" not in events
    assert _payload(out)["error_type"] == "MappingValidationFailed"


def test_scd2_early_materialization_issues_do_not_reject_a_repaired_mapping(tmp_path):
    """The early call discards its issues and repairs access points; only the final list decides."""
    _, out, events, _ = _scd2(tmp_path, [True, False], materialize=True, issues=[["FATAL"], []])
    assert _payload(out)["status"] == "passed"
    assert events == ["validate", "setLKM:LATE_AP", "validate", "commit"]


# --- update_mapping.groovy --------------------------------------------------------------------


def test_update_validation_errors_never_reach_commit(tmp_path):
    code, out, events, err = _update(tmp_path, [True], issues=[["ERROR"]])
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


@pytest.mark.parametrize("levels", [["FATAL"], ["ERROR"], ["WARNING", "FATAL"], ["INFO", "ERROR"]])
def test_update_false_with_error_or_fatal_issue_never_reaches_commit(tmp_path, levels):
    code, out, events, err = _update(tmp_path, [False], issues=[levels])
    assert code == 1
    assert events == ["validate", "rollback"]
    assert _payload(out) == {
        "status": "error",
        "action": "update_mapping",
        "error_type": "MappingValidationFailed",
    }
    assert "SUCCESS" not in out
    _assert_no_leak(out, err)


@pytest.mark.parametrize("levels", [[], ["WARNING"], ["INFO"], ["WARNING", "INFO"]])
def test_update_false_with_only_lower_level_issues_commits(tmp_path, levels):
    code, out, events, _ = _update(tmp_path, [False], issues=[levels])
    assert code == 0
    assert events == ["validate", "commit"]
    assert "SUCCESS: Mapping 'M' updated" in out


@pytest.mark.parametrize("levels", [[], ["WARNING"]])
def test_update_true_without_error_issues_is_still_rejected(tmp_path, levels):
    code, out, events, _ = _update(tmp_path, [True], issues=[levels])
    assert code == 1
    assert events == ["validate", "rollback"]
    assert _payload(out)["error_type"] == "MappingValidationFailed"


def test_update_no_op_does_not_consult_the_validation_gate(tmp_path):
    code, _, events, _ = _update(tmp_path, [False], changed=False, issues=[["FATAL"]])
    assert code == 0
    assert events == ["commit"]


# --- create_mapping.groovy --------------------------------------------------------------------


@pytest.mark.parametrize("replacing", [True, False])
@pytest.mark.parametrize("levels", [["FATAL"], ["ERROR"], ["WARNING", "FATAL"], ["INFO", "ERROR"]])
def test_create_false_with_error_or_fatal_issue_blocks_before_remove_and_commit(tmp_path, levels, replacing):
    code, out, events, err = _create(tmp_path, [False], replacing=replacing, issues=[levels])
    assert code == 1
    assert events == ["validate", "rollback"]
    payload = _payload(out)
    assert payload["status"] == "error"
    assert payload["action"] == "create_mapping"
    assert payload["replaced"] is False
    assert payload["kept_existing_on_failure"] is replacing
    assert payload["error_type"] == "IllegalStateException"
    assert any(message.startswith(("ERROR: ", "FATAL: ")) for message in payload["validation_messages"])
    assert "physical_design" in payload
    assert "SUCCESS" not in out
    assert "hunter2" not in out + err


@pytest.mark.parametrize("levels", [[], ["WARNING"], ["INFO"], ["WARNING", "INFO"]])
def test_create_false_with_only_lower_level_issues_replaces_and_commits(tmp_path, levels):
    code, out, events, _ = _create(tmp_path, [False], issues=[levels])
    assert code == 0
    assert events == ["validate", "remove", "flush", "commit"]
    payload = _payload(out)
    assert payload["status"] == "passed"
    assert payload["replaced"] is True
    assert payload["kept_existing_on_failure"] is False


def test_create_new_mapping_with_warning_only_commits_without_remove(tmp_path):
    code, out, events, _ = _create(tmp_path, [False], replacing=False, issues=[["WARNING"]])
    assert code == 0
    assert events == ["validate", "commit"]
    assert _payload(out)["replaced"] is False


@pytest.mark.parametrize("levels", [[], ["WARNING"]])
def test_create_true_without_error_issues_is_still_rejected(tmp_path, levels):
    code, out, events, _ = _create(tmp_path, [True], issues=[levels])
    assert code == 1
    assert events == ["validate", "rollback"]
    assert _payload(out)["kept_existing_on_failure"] is True


def test_create_validate_exception_keeps_the_existing_mapping(tmp_path):
    code, out, events, err = _create(tmp_path, [False], explode=True)
    assert code == 1
    assert events == ["validate", "rollback"]
    payload = _payload(out)
    assert payload["error_type"] == "RuntimeException"
    assert payload["kept_existing_on_failure"] is True
    assert "hunter2" not in out + err


# --- compile against the installed SDK ---------------------------------------------------------


@pytest.mark.parametrize("name", ["create_mapping.groovy", "scd2_mapping_create.groovy", "update_mapping.groovy"])
def test_changed_scripts_compile_against_the_installed_sdk(name):
    """Compile only: resolves every import. Nothing connects and the script body does not run."""
    java = Path(odi_orchestrator._JAVA_BIN)
    sdk = Path(odi_orchestrator._ODI_SDK_LIB) / "odi-core.jar"
    if not (java.is_file() and sdk.is_file() and Path(odi_orchestrator._GROOVY_JAR).is_file()):
        pytest.skip("Local SDK or Groovy compiler is unavailable.")
    done = subprocess.run(
        [
            str(java), "-cp", OdiOrchestrator()._build_classpath(), "groovy.ui.GroovyMain", "-e",
            "new GroovyClassLoader().parseClass(new File(args[0])); println('SDK_COMPILE_OK')",
            str(ROOT / name),
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SDK_COMPILE_OK" in done.stdout


# --- static guard for CI, where Groovy is absent and the behaviour tests above skip ------------


@pytest.mark.parametrize("name", ["scd2_mapping_create.groovy", "update_mapping.groovy"])
def test_final_gate_directly_precedes_the_changed_mapping_commit(name):
    source = _text(name)
    assert "import oracle.odi.domain.root.RootIssue" in source
    gate = source.rindex("mapping.validate(finalValidationIssues, false)")
    block = source[gate:source.index("txnMgr.commit(txnStatus)", gate)]
    assert "RootIssue.isErrorOrFatal(finalValidationIssues)" in block
    assert "MappingValidationFailed" in block
    assert source.rfind("mapping.validate()") < gate
    assert "new ArrayList(), false" not in source
    # Issue text never reaches the output: the list is only filled and read through these calls.
    assert source.count("finalValidationIssues") == 3


def test_create_mapping_gate_uses_both_signals_before_remove_and_commit():
    source = _text("create_mapping.groovy")
    assert "import oracle.odi.domain.root.RootIssue" in source
    helper = _between(source, "def mappingHasErrors = {", "def collectDesignFailure")
    assert "mp.validate(issues, false) == true || RootIssue.isErrorOrFatal(issues)" in helper
    gate = source.index("mappingHasErrors(mapping, validationIssues)")
    assert gate < source.index("em.remove(existingMapping)") < source.index("txnMgr.commit(txnStatus)")
