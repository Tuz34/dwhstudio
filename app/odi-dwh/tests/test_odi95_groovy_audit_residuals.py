"""ODI-95 text contracts for the two Groovy audit residuals.

The unknown ETL_BATCH_ID datatype must fail closed before assignment, and a
filter rebuild must restore the previous expression instead of writing "0".
Python turns those fixed codes into fixed sentences. Groovy is not executed.
"""

from __future__ import annotations

import json
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
    assert "throw new audit_expression_not_restored()" in capture_helper
    assert "throw new audit_expression_not_restored()" in restore_helper
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


def test_update_route_maps_unrestored_expression_to_a_fixed_sentence(app_client, monkeypatch):
    import mapping_generator

    line = (
        '{"status":"error","action":"update_mapping","error_type":"'
        + EXPRESSION_NOT_RESTORED
        + '"}'
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
    _assert_fixed(
        response.get_json(),
        response.get_data(as_text=True),
        EXPRESSION_NOT_RESTORED,
        EXPRESSION_NOT_RESTORED_SENTENCE,
    )


def test_update_runner_keeps_only_the_fixed_code(monkeypatch):
    line = (
        '{"status":"error","action":"update_mapping","error_type":"'
        + EXPRESSION_NOT_RESTORED
        + '"}'
    )
    _fake_process(monkeypatch, "progress " + MARKER + "\n" + line + "\n")
    with odi_orchestrator.groovy_approval_context(True):
        output = OdiOrchestrator().run_groovy("update_mapping.groovy", {})
    assert output == f"Hata: ODI Groovy action failed. error_type={EXPRESSION_NOT_RESTORED}"
    assert MARKER not in output


def test_other_update_errors_stay_generic(monkeypatch):
    line = '{"status":"error","action":"update_mapping","error_type":"MappingValidationFailed"}\n'
    _fake_process(monkeypatch, line)
    with odi_orchestrator.groovy_approval_context(True):
        output = OdiOrchestrator().run_groovy("update_mapping.groovy", {})
    assert output == "Hata: ODI Groovy action failed. error_type=GroovyProcessError"
