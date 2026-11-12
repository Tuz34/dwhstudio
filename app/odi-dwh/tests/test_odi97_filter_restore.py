"""ODI-97: a filter rebuild says which restore check failed, and still fails closed.

The public sentence stays fixed. ``error_reason`` is a separate allowlisted token.
A setter that stores a different form still fails the exact read-back. getText()
returns the stored argument, so a normalised-form comparator is not used.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

import odi_orchestrator
from odi_orchestrator import OdiOrchestrator
from tests.test_odi95_groovy_audit_residuals import (
    EXPRESSION_NOT_RESTORED,
    EXPRESSION_NOT_RESTORED_SENTENCE,
    MARKER,
    _between,
    _fake_process,
    _run_groovy,
    _text,
)

REASONS = (
    "missing_attribute",
    "duplicate_attribute",
    "null_attribute",
    "read_failed",
    "write_failed",
    "readback_mismatch",
    "count_mismatch",
)
HOSTILE = "CASE_WHEN_AUDIT_SECRET"
GENERIC = "Hata: ODI Groovy action failed. error_type=GroovyProcessError"


def _line(**overrides) -> str:
    payload = {
        "status": "error",
        "action": "update_mapping",
        "error_type": EXPRESSION_NOT_RESTORED,
        "reason": "readback_mismatch",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _run_update(monkeypatch, stdout: str) -> str:
    _fake_process(monkeypatch, stdout)
    with odi_orchestrator.groovy_approval_context(True):
        return OdiOrchestrator().run_groovy("update_mapping.groovy", {})


def test_restore_throws_use_only_the_closed_reason_literals():
    source = _text("update_mapping.groovy")
    found = re.findall(r"throw new audit_expression_not_restored\(([^)\n]*)\)", source)
    assert set(found) == {f'"{reason}"' for reason in REASONS}
    catch = source[source.rindex("} catch (Exception e) {") :]
    assert "RESTORE_FAILURE_REASONS.contains(restoreReason)" in catch
    assert "e.getMessage()" not in catch
    assert "getText()" not in catch
    helpers = _between(source, "def captureTargetExpressions", "def odi = null")
    assert helpers.count("setExpressionText(") == 1
    assert "written != text" in helpers


@pytest.mark.parametrize("reason", REASONS)
def test_each_allowlisted_reason_is_returned_and_logged(monkeypatch, caplog, reason):
    caplog.set_level(logging.ERROR, logger="odi_orchestrator")
    output = _run_update(monkeypatch, HOSTILE + "\n" + _line(reason=reason) + "\n")
    assert output == (
        "Hata: ODI Groovy action failed. "
        f"error_type={EXPRESSION_NOT_RESTORED} error_reason={reason}"
    )
    assert HOSTILE not in output
    assert HOSTILE not in caplog.text
    assert f"class={EXPRESSION_NOT_RESTORED} reason={reason}" in caplog.text


@pytest.mark.parametrize(
    "stdout",
    [
        _line(reason=HOSTILE),
        _line(reason="readback_mismatch "),
        _line(reason="READBACK_MISMATCH"),
        _line(reason=""),
        _line(reason=None),
        json.dumps(
            {
                "status": "error",
                "action": "update_mapping",
                "error_type": EXPRESSION_NOT_RESTORED,
            }
        ),
        _line(expression=HOSTILE),
        _line(error_type="MappingValidationFailed", reason="readback_mismatch"),
        HOSTILE,
    ],
)
def test_unknown_or_incomplete_reason_stays_generic(monkeypatch, caplog, stdout):
    caplog.set_level(logging.ERROR, logger="odi_orchestrator")
    output = _run_update(monkeypatch, HOSTILE + "\n" + stdout + "\n")
    assert output == GENERIC
    assert HOSTILE not in output
    assert EXPRESSION_NOT_RESTORED not in output
    assert "error_reason=" not in output
    assert HOSTILE not in caplog.text
    assert f"class={EXPRESSION_NOT_RESTORED}" not in caplog.text


def test_update_route_rejects_an_unknown_reason(app_client, monkeypatch):
    import mapping_generator

    _fake_process(monkeypatch, HOSTILE + "\n" + _line(reason=HOSTILE) + "\n")
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
    assert body["overall_status"] == "FAILED"
    assert body["error"] == "mapping update failed."
    assert "error_reason" not in body
    assert body.get("error_code") != EXPRESSION_NOT_RESTORED
    assert EXPRESSION_NOT_RESTORED_SENTENCE not in rendered
    assert HOSTILE not in rendered
    assert MARKER not in rendered


def _reason_script() -> str:
    source = _text("update_mapping.groovy")
    body = _between(source, "class audit_expression_not_restored extends ", "// ── CONNECTION")
    return "import groovy.json.JsonOutput\n" + body + r'''
class HeldText {
    String text
    String getText() { return text }
}
class NamedAttr {
    String name
    HeldText expression
    boolean rewrite = false
    boolean stick = true
    boolean explodeSet = false
    boolean explodeGet = false
    int sets = 0
    String getName() { return name }
    Object getExpression() {
        if (explodeGet && sets > 0) throw new IllegalStateException("hidden-get")
        return expression
    }
    void setExpressionText(String value) {
        sets = sets + 1
        if (explodeSet) throw new IllegalStateException("hidden-set")
        if (!stick) return
        expression = new HeldText(text: rewrite ? (value + " ") : value)
    }
}
class BlankAttr {
    String getName() { return " " }
    Object getExpression() { return null }
    void setExpressionText(String value) {}
}
class ProbeComp {
    List attributes = []
    List getAttributes() { return attributes }
}
class BlowComp {
    List getAttributes() { throw new IllegalStateException("hidden-attrs") }
}
NamedAttr exact(String name) {
    return new NamedAttr(name: name, expression: new HeldText(text: "OLD"))
}
Map savedOf(Map pairs) {
    Map saved = new LinkedHashMap()
    pairs.each { key, value -> saved.put(key, value) }
    return saved
}
def outcome = { String label, Closure body ->
    boolean rejected = false
    String errorClass = ""
    String reason = ""
    boolean textMatches = false
    try {
        textMatches = body.call() == true
    } catch (Throwable raised) {
        rejected = true
        errorClass = raised.getClass().getSimpleName()
        if (raised instanceof audit_expression_not_restored) reason = raised.reason ?: ""
    }
    return [label: label, rejected: rejected, error_class: errorClass, reason: reason, text_matches: textMatches]
}
def rows = []
rows << outcome("missing_attribute") {
    restoreTargetExpressions(
        new ProbeComp(attributes: [exact("COL_A")]),
        savedOf(["COL_A": "EXPR_A", "COL_B": "EXPR_B"]))
    return false
}
rows << outcome("duplicate_attribute") {
    restoreTargetExpressions(
        new ProbeComp(attributes: [exact("COL_A"), exact("COL_A")]),
        savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("null_attribute") {
    restoreTargetExpressions(new ProbeComp(attributes: [null]), savedOf([:]))
    return false
}
rows << outcome("blank_name") {
    restoreTargetExpressions(new ProbeComp(attributes: [new BlankAttr()]), savedOf([:]))
    return false
}
rows << outcome("read_failed") {
    restoreTargetExpressions(null, savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("read_failed_attributes") {
    restoreTargetExpressions(new BlowComp(), savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("write_failed") {
    def attr = exact("COL_A")
    attr.explodeSet = true
    restoreTargetExpressions(new ProbeComp(attributes: [attr]), savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("write_failed_after_set") {
    def attr = exact("COL_A")
    attr.explodeGet = true
    restoreTargetExpressions(new ProbeComp(attributes: [attr]), savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("readback_mismatch") {
    def attr = exact("COL_A")
    attr.stick = false
    restoreTargetExpressions(new ProbeComp(attributes: [attr]), savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("null_text_mismatch") {
    def attr = exact("COL_A")
    attr.expression = new HeldText(text: "STILL_THERE")
    restoreTargetExpressions(new ProbeComp(attributes: [attr]), savedOf(["COL_A": null]))
    return false
}
rows << outcome("count_mismatch") {
    restoreTargetExpressions(
        new ProbeComp(attributes: [exact("COL_EXTRA")]),
        savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("normalised_form_differs") {
    def attr = exact("COL_A")
    attr.rewrite = true
    restoreTargetExpressions(new ProbeComp(attributes: [attr]), savedOf(["COL_A": "EXPR_A"]))
    return false
}
rows << outcome("exact_form_matches") {
    def attr = exact("COL_A")
    restoreTargetExpressions(new ProbeComp(attributes: [attr]), savedOf(["COL_A": "EXPR_A"]))
    return attr.expression?.text == "EXPR_A"
}
rows << outcome("capture_duplicate_attribute") {
    captureTargetExpressions(new ProbeComp(attributes: [exact("COL_A"), exact("col_a")]))
    return false
}
println JsonOutput.toJson([rows: rows])
'''


def test_fake_sdk_names_each_restore_failure_and_keeps_the_exact_readback(tmp_path):
    """Each closed reason is raised. A rewritten stored form still does not pass."""
    report = _run_groovy(tmp_path, _reason_script())
    rows = {row["label"]: row for row in report["rows"]}
    for reason in REASONS:
        row = rows[reason]
        assert row["rejected"] is True, row
        assert row["error_class"] == "audit_expression_not_restored", row
        assert row["reason"] == reason, row
        assert row["text_matches"] is False
    for label, reason in (
        ("blank_name", "null_attribute"),
        ("read_failed_attributes", "read_failed"),
        ("write_failed_after_set", "write_failed"),
        ("null_text_mismatch", "readback_mismatch"),
        ("normalised_form_differs", "readback_mismatch"),
        ("capture_duplicate_attribute", "duplicate_attribute"),
    ):
        row = rows[label]
        assert row["rejected"] is True, row
        assert row["error_class"] == "audit_expression_not_restored", row
        assert row["reason"] == reason, row
    matched = rows["exact_form_matches"]
    assert matched["rejected"] is False, matched
    assert matched["text_matches"] is True
    assert HOSTILE not in json.dumps(report)
