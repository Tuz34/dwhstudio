"""ODI-95 contracts for the two Groovy audit residuals.

The unknown ETL_BATCH_ID datatype must fail closed before assignment, and a
filter rebuild must restore the previous expression instead of writing "0".
Python turns those fixed codes into fixed sentences. The character-code
allowlist and the restore read-back run on fake attributes, not on ODI.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

import odi_orchestrator
from odi_orchestrator import OdiOrchestrator

ROOT = Path(__file__).resolve().parents[1]
GROOVY = ROOT / "odi_automation"
MARKER = "ODI95_GROOVY_MESSAGE"
TYPE_UNKNOWN = "audit_type_unknown"
EXPRESSION_NOT_RESTORED = "audit_expression_not_restored"
TYPE_UNKNOWN_SENTENCE = "The ETL_BATCH_ID datatype is unknown. Nothing was written."
EXPRESSION_NOT_RESTORED_SENTENCE = (
    "The existing target expression could not be kept. Nothing was committed."
)


def _text(name: str) -> str:
    return (GROOVY / name).read_text(encoding="utf-8")


def _unknown_type_branch(source: str) -> str:
    start = source.index("if (typeCode == null || typeCode.length() == 0) {")
    end = source.index("\n        }", start)
    return source[start:end]


def test_unknown_datatype_branch_emits_the_fixed_error_before_assignment():
    source = _text("create_mapping.groovy")
    branch = _unknown_type_branch(source)
    assert "throw new audit_type_unknown()" in branch
    assert "setExpressionText" not in branch
    assert "return" not in branch
    assert source.index("throw new audit_type_unknown()") < source.index("setExpressionText")
    guard = source.index("if (needsSessionBatch(tAttr)) {\n            typedSessionBatch(tAttr)")
    assert guard < source.index("tAttr.setExpressionText(typedSessionBatch(tAttr))")
    catch = source[source.rindex("} catch (Exception e) {"):]
    assert '"error_type":"' in catch
    assert "System.exit(1)" in catch
    assert "txnMgr.rollback(txnStatus)" in catch
    assert "txnMgr.commit" not in catch


def test_known_character_branch_still_returns_the_quoted_token():
    """Control: a known non-numeric code still ends on the quoted session token."""
    source = _text("create_mapping.groovy")
    start = source.index('if (typeCode == "NUMBER")')
    tail = source[start:source.index("\n    }\n", start)]
    assert "return quoted" in tail
    assert "setExpressionText" not in tail
    assert 'typeCode == "BINARY_DOUBLE"' in tail
    assert 'typeCode == "FLOAT"' in tail


def test_update_filter_rebuilds_do_not_assign_a_zero_expression():
    source = _text("update_mapping.groovy")
    assert 'setExpressionText("0")' not in source
    assert "setExpressionText('0')" not in source
    assert "class audit_expression_not_restored extends RuntimeException" in source
    catch = source[source.rindex("} catch (Exception e) {"):]
    assert "txnMgr.rollback(txnStatus)" in catch
    assert "System.exit(1)" in catch
    assert "txnMgr.commit" not in catch


def test_both_filter_rebuilds_capture_and_restore_the_previous_expression():
    source = _text("update_mapping.groovy")
    sites = (
        ("mapping.removeComponent(downstream)", "restoreTargetExpressions(newDown, savedExpressions)"),
        ("mapping.removeComponent(removeTgt)", "restoreTargetExpressions(newTgt, savedExpressions)"),
    )
    for removal, restore in sites:
        region_start = source.rindex("captureTargetExpressions(", 0, source.index(removal))
        region = source[region_start:source.index(restore, region_start) + len(restore)]
        assert region.index("captureTargetExpressions(") < region.index(removal) < region.index(restore)
        assert "throw denied" in source[source.index(restore):source.index(restore) + 500]
    capture_helper = source[
        source.index("def captureTargetExpressions"):source.index("def restoreTargetExpressions")
    ]
    restore_helper = source[source.index("def restoreTargetExpressions"):source.index("def odi = null")]
    assert 'throw new audit_expression_not_restored("read_failed")' in capture_helper
    assert 'throw new audit_expression_not_restored("readback_mismatch")' in restore_helper
    assert "throw new audit_expression_not_restored()" not in source
    assert "written != text" in restore_helper
    assert "setExpressionText" not in capture_helper
    assert 'setExpressionText("0")' not in capture_helper + restore_helper
    assert "saved.put(key, text)" in capture_helper
    assert "setExpressionText(text)" in restore_helper
    assert "getExpression()" in capture_helper and "getText()" in capture_helper


def _fake_process(monkeypatch, stdout: str):
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(OdiOrchestrator, "_build_classpath", lambda _self: "cp")
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: "local.properties")
    monkeypatch.setattr(
        odi_orchestrator.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout, ""),
    )


def _validated_params() -> dict:
    return {
        "mapping_name": "MAP_X",
        "src_model": "ORACLE_SOURCE",
        "src_table": "EMPLOYEES",
        "tgt_model": "ORACLE_ODS",
        "tgt_table": "ODS_HR_EMPLOYEES",
        "tgt_owner": "DWH",
        "needs_skey": False,
        "integration_type": "CONTROL_APPEND",
        "ikm_name": "IKM Oracle Insert",
    }


def _stub_generate_front(monkeypatch):
    import mapping_generator

    generator = mapping_generator.MappingGenerator
    monkeypatch.setattr(generator, "validate_params", lambda self, _params: _validated_params())
    monkeypatch.setattr(generator, "_decide_effective_audit", lambda self, _params: None)
    monkeypatch.setattr(generator, "_preflight_odi_repository", lambda self, _params: {"status": "passed"})
    monkeypatch.setattr(generator, "_resolve_odi_repository_bindings", lambda self, _params: {})
    monkeypatch.setattr(generator, "_create_target_table", lambda self, _params: {"status": "SUCCESS"})
    monkeypatch.setattr(generator, "_register_odi_datastores", lambda self, _params: {})
    monkeypatch.setattr(generator, "_ensure_odi_repository_bindings", lambda self, _params: None)
    monkeypatch.setattr(generator, "_resolved_odi_source_model_code", lambda self, _params: "ORACLE_SOURCE")
    monkeypatch.setattr(generator, "_resolved_odi_target_model_code", lambda self, _params: "ORACLE_ODS")
    monkeypatch.setattr(generator, "_log_to_mapping_log", lambda self, *_args, **_kwargs: None)


def _assert_fixed(body: dict, rendered: str, code: str, sentence: str):
    assert body["overall_status"] == "FAILED"
    assert body["error_code"] == code
    assert body["error"] == sentence
    assert body["error_type"] == "OperationError"
    assert MARKER not in rendered
    assert "Hata:" not in rendered
    assert code not in body["error"]


@pytest.mark.parametrize(
    "path,stdout,code,sentence",
    [
        (
            "/api/mapping/generate?engine=classic",
            None,
            TYPE_UNKNOWN,
            TYPE_UNKNOWN_SENTENCE,
        ),
    ],
)
def test_create_route_maps_unknown_batch_type_to_a_fixed_sentence(app_client, monkeypatch, path, stdout, code, sentence):
    payload = {
        "status": "error",
        "action": "create_mapping",
        "replaced": False,
        "kept_existing_on_failure": False,
        "error_type": TYPE_UNKNOWN,
        "validation_messages": [MARKER],
        "physical_design": {"note": MARKER},
    }
    _fake_process(monkeypatch, MARKER + "\n" + json.dumps(payload) + "\n")
    _stub_generate_front(monkeypatch)
    response = app_client.post(path, json={"params": {"mapping_name": "MAP_X"}, "approved": True})
    assert response.status_code == 200
    _assert_fixed(response.get_json(), response.get_data(as_text=True), code, sentence)


def test_update_route_maps_unrestored_expression_to_a_fixed_sentence(app_client, monkeypatch, caplog):
    import mapping_generator

    caplog.set_level(logging.ERROR, logger="odi_orchestrator")
    line = json.dumps(
        {
            "status": "error",
            "action": "update_mapping",
            "error_type": EXPRESSION_NOT_RESTORED,
            "reason": "readback_mismatch",
        }
    )
    _fake_process(monkeypatch, MARKER + " expression text\n" + line + "\n")
    generator = mapping_generator.MappingGenerator
    monkeypatch.setattr(generator, "_check_mapping_exists", lambda self, name: (True, name))
    monkeypatch.setattr(generator, "_take_snapshot_for_update", lambda self, *_args, **_kwargs: {"snap_id": 1})
    monkeypatch.setattr(generator, "_populate_lineage", lambda self, *_args, **_kwargs: {})
    monkeypatch.setattr(generator, "_log_to_mapping_log", lambda self, *_args, **_kwargs: None)
    response = app_client.post(
        "/api/mapping/update",
        json={
            "mapping_name": "MAP_X",
            "approved": True,
            "changes": {"filter_action": "add", "filter_condition": "SRC.STATUS = 'A'"},
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    rendered = response.get_data(as_text=True)
    _assert_fixed(body, rendered, EXPRESSION_NOT_RESTORED, EXPRESSION_NOT_RESTORED_SENTENCE)
    assert body["error_reason"] == "readback_mismatch"
    assert "reason=readback_mismatch" in caplog.text
    assert f"class={EXPRESSION_NOT_RESTORED}" in caplog.text
    assert MARKER not in caplog.text


def test_update_runner_keeps_only_the_fixed_code(monkeypatch):
    line = json.dumps(
        {
            "status": "error",
            "action": "update_mapping",
            "error_type": EXPRESSION_NOT_RESTORED,
            "reason": "readback_mismatch",
        }
    )
    _fake_process(monkeypatch, "progress " + MARKER + "\n" + line + "\n")
    with odi_orchestrator.groovy_approval_context(True):
        output = OdiOrchestrator().run_groovy("update_mapping.groovy", {})
    assert output == (
        "Hata: ODI Groovy action failed. "
        f"error_type={EXPRESSION_NOT_RESTORED} error_reason=readback_mismatch"
    )
    assert MARKER not in output


def test_other_update_errors_stay_generic(monkeypatch):
    line = '{"status":"error","action":"update_mapping","error_type":"MappingValidationFailed"}\n'
    _fake_process(monkeypatch, line)
    with odi_orchestrator.groovy_approval_context(True):
        output = OdiOrchestrator().run_groovy("update_mapping.groovy", {})
    assert output == "Hata: ODI Groovy action failed. error_type=GroovyProcessError"


def _between(source: str, start: str, end: str) -> str:
    at = source.index(start)
    return source[at:source.index(end, at)]


def _run_groovy(tmp_path: Path, script: str) -> dict:
    """Run a sliced production closure against fake attributes. No repository."""
    from tests import dim_product_mapping_fake_sdk as sdk

    java = sdk.java_binary()
    if java is None or not sdk.GROOVY_JAR.is_file():
        pytest.skip("Local Java/Groovy unavailable; fake-SDK audit checks are not live-proven")
    path = tmp_path / "odi95_fake_sdk.groovy"
    path.write_text(script, encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP", "PATH") if key in os.environ}
    done = subprocess.run(
        [java, "-cp", str(sdk.GROOVY_JAR), "groovy.ui.GroovyMain", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:] or done.stdout[-2000:])
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _type_script() -> str:
    source = _text("create_mapping.groovy")
    class_at = source.index("class audit_type_unknown extends ")
    prelude = source[class_at:source.index("\n}", class_at) + 2]
    closure = _between(
        source,
        '    String sessionBatchLiteral = ',
        "    // Fail before the assignment loop.",
    )
    return "import groovy.json.JsonOutput\n" + prelude + "\n" + closure + r'''
class TypeCode {
    String code
    String getCode() { return code }
}
class ProbeAttr {
    Object dataType
    boolean wrote = false
    String expression
    Object getDataType() { return dataType }
    Object getLength() { return null }
    Object getScale() { return null }
    void setExpressionText(String text) {
        wrote = true
        expression = text
    }
}
def runCase = { String label, ProbeAttr attr ->
    boolean rejected = false
    String errorClass = ""
    try {
        String text = typedSessionBatch(attr)
        attr.setExpressionText(text)
    } catch (Throwable raised) {
        rejected = true
        errorClass = raised.getClass().getSimpleName()
    }
    return [label: label, rejected: rejected, error_class: errorClass, assigned: attr.expression, wrote: attr.wrote]
}
def rows = []
rows << runCase("UNKNOWN", new ProbeAttr(dataType: new TypeCode(code: "UNKNOWN")))
rows << runCase("NUMERIC", new ProbeAttr(dataType: new TypeCode(code: "NUMERIC")))
rows << runCase("empty", new ProbeAttr(dataType: new TypeCode(code: "")))
rows << runCase("null", new ProbeAttr())
rows << runCase("DATE", new ProbeAttr(dataType: new TypeCode(code: "DATE")))
rows << runCase("LONG RAW", new ProbeAttr(dataType: new TypeCode(code: "LONG RAW")))
rows << runCase("VARCHAR2", new ProbeAttr(dataType: new TypeCode(code: "VARCHAR2")))
rows << runCase("VARCHAR", new ProbeAttr(dataType: new TypeCode(code: "VARCHAR")))
rows << runCase("CHAR", new ProbeAttr(dataType: new TypeCode(code: "CHAR")))
rows << runCase("NVARCHAR2", new ProbeAttr(dataType: new TypeCode(code: "NVARCHAR2")))
rows << runCase("NCHAR", new ProbeAttr(dataType: new TypeCode(code: "NCHAR")))
rows << runCase("CLOB", new ProbeAttr(dataType: new TypeCode(code: "CLOB")))
rows << runCase("NCLOB", new ProbeAttr(dataType: new TypeCode(code: "NCLOB")))
rows << runCase("LONG", new ProbeAttr(dataType: new TypeCode(code: "LONG")))
rows << runCase("NUMBER", new ProbeAttr(dataType: new TypeCode(code: "NUMBER")))
println JsonOutput.toJson([quoted: sessionBatchLiteral, rows: rows])
'''


def test_nonempty_unknown_datatype_codes_fail_before_assignment(tmp_path):
    """UNKNOWN, NUMERIC, empty, and null are rejected. VARCHAR2 stays quoted."""
    report = _run_groovy(tmp_path, _type_script())
    quoted = report["quoted"]
    rows = {row["label"]: row for row in report["rows"]}
    problems = []
    for label in ("UNKNOWN", "NUMERIC", "empty", "null", "DATE", "LONG RAW"):
        row = rows[label]
        if not (
            row["rejected"] is True
            and row["error_class"] == "audit_type_unknown"
            and row["wrote"] is False
            and row["assigned"] is None
        ):
            problems.append((label, row))
    for label in ("VARCHAR2", "VARCHAR", "CHAR", "NVARCHAR2", "NCHAR", "CLOB", "NCLOB", "LONG"):
        row = rows[label]
        if row["rejected"] or row["wrote"] is not True or row["assigned"] != quoted:
            problems.append((label, row))
    number = rows["NUMBER"]
    if number["rejected"] or number["wrote"] is not True or number["assigned"] != f"TO_NUMBER({quoted})":
        problems.append(("NUMBER", number))
    assert problems == []


def _restore_script() -> str:
    source = _text("update_mapping.groovy")
    body = _between(source, "class audit_expression_not_restored extends ", "// ── CONNECTION")
    return "import groovy.json.JsonOutput\n" + body + r'''
class HeldText {
    String text
    String getText() { return text }
}
class StickyAttr {
    String name
    HeldText expression
    String getName() { return name }
    Object getExpression() { return expression }
    void setExpressionText(String value) { expression = new HeldText(text: value) }
}
class DropAttr {
    String name
    HeldText expression
    int sets = 0
    String getName() { return name }
    Object getExpression() { return expression }
    void setExpressionText(String value) { sets = sets + 1 }
}
class ProbeComp {
    List attributes = []
    List getAttributes() { return attributes }
}
String kept = "CAST(TO_NUMBER('<%=odiRef.getSession(\"SESS_NO\")%>') AS NUMBER(19,0))"
def sticky = new StickyAttr(name: "ETL_BATCH_ID", expression: new HeldText(text: "0"))
def stickySaved = new LinkedHashMap()
stickySaved.put("ETL_BATCH_ID", kept)
boolean stickyRejected = false
String stickyClass = ""
try {
    restoreTargetExpressions(new ProbeComp(attributes: [sticky]), stickySaved)
} catch (Throwable raised) {
    stickyRejected = true
    stickyClass = raised.getClass().getSimpleName()
}
def dropped = new DropAttr(name: "ETL_BATCH_ID", expression: new HeldText(text: "0"))
def droppedSaved = new LinkedHashMap()
droppedSaved.put("ETL_BATCH_ID", kept)
boolean droppedRejected = false
String droppedClass = ""
String droppedReason = ""
try {
    restoreTargetExpressions(new ProbeComp(attributes: [dropped]), droppedSaved)
} catch (Throwable raised) {
    droppedRejected = true
    droppedClass = raised.getClass().getSimpleName()
    if (raised instanceof audit_expression_not_restored) droppedReason = raised.reason
}
println JsonOutput.toJson([
    kept: kept,
    sticky_rejected: stickyRejected,
    sticky_class: stickyClass,
    sticky_text: sticky.expression?.text,
    dropped_rejected: droppedRejected,
    dropped_class: droppedClass,
    dropped_reason: droppedReason,
    dropped_sets: dropped.sets,
    dropped_text: dropped.expression?.text,
])
'''


def test_restore_readback_rejects_when_set_expression_text_does_not_stick(tmp_path):
    """A setExpressionText call that leaves the old text must fail closed."""
    report = _run_groovy(tmp_path, _restore_script())
    assert report["sticky_rejected"] is False, report
    assert report["sticky_text"] == report["kept"]
    assert report["dropped_sets"] == 1
    assert report["dropped_text"] == "0"
    assert report["dropped_rejected"] is True
    assert report["dropped_class"] == "audit_expression_not_restored"
    assert report["dropped_reason"] == "readback_mismatch"
