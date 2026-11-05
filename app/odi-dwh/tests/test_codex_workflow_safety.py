from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_TEMPLATES = PROJECT_ROOT / "docs" / "agent-skills"
SKILL_NAMES = ("odi-review", "odi-readonly-ui-smoke")


def _template_file(name: str, *parts: str) -> Path:
    path = SKILL_TEMPLATES.joinpath(name, *parts)
    assert PROJECT_ROOT.resolve() in path.resolve().parents
    return path


def test_repository_skill_templates_are_explicit_only():
    for name in SKILL_NAMES:
        skill = _template_file(name, "SKILL.md")
        metadata = _template_file(name, "agents", "openai.yaml")

        assert skill.is_file()
        assert metadata.is_file()
        text = metadata.read_text(encoding="utf-8")
        assert "allow_implicit_invocation: false" in text
        assert "allow_implicit_invocation: true" not in text


def test_review_skill_template_is_targeted_and_read_only():
    text = _template_file("odi-review", "SKILL.md").read_text(encoding="utf-8")

    assert "Read the exact Linear issue" in text
    assert "Never read `docs/archive` wholesale" in text
    assert "Do not modify source" in text
    assert "Do not inspect `.env`" in text


def test_ui_skill_template_stops_when_app_is_down_and_forbids_persistent_actions():
    text = _template_file("odi-readonly-ui-smoke", "SKILL.md").read_text(encoding="utf-8")

    assert "do not start or restart it" in text
    assert "Never click or programmatically invoke" in text
    for label in ("Apply DB", "Sync ODI", "Run Scenario", "Rollback", "Autofix"):
        assert label in text
    assert "Do not call write endpoints directly" in text


def test_workflow_doc_keeps_automation_dormant_and_hooks_report_only():
    text = (PROJECT_ROOT / "docs" / "codex-workflow-safety.md").read_text(encoding="utf-8")

    assert "only active mode is\n`report_only`" in text
    assert "Blocking activation is not\nimplemented" in text
    assert "No recurring task is registered or enabled" in text
    assert "Idle checks use no LLM" in text


def test_workflow_doc_keeps_skill_templates_uninstalled():
    text = (PROJECT_ROOT / "docs" / "codex-workflow-safety.md").read_text(encoding="utf-8")

    assert "app/odi-dwh/docs/agent-skills/" in text
    assert "do not install or activate them" in text
    assert "Copying a template into a local skill directory is optional" in text
