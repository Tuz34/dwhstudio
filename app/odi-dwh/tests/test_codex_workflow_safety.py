from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODEX_SKILLS = Path.home() / ".codex" / "skills"


def test_on_demand_skills_are_installed_and_explicit_only():
    for name in ("odi-review", "odi-readonly-ui-smoke"):
        skill = CODEX_SKILLS / name / "SKILL.md"
        metadata = CODEX_SKILLS / name / "agents" / "openai.yaml"

        assert skill.is_file()
        assert metadata.is_file()
        assert "allow_implicit_invocation: false" in metadata.read_text(encoding="utf-8")


def test_review_skill_is_targeted_and_read_only():
    text = (CODEX_SKILLS / "odi-review" / "SKILL.md").read_text(encoding="utf-8")

    assert "Read the exact Linear issue" in text
    assert "Never read `docs/archive` wholesale" in text
    assert "Do not modify source" in text
    assert "Do not inspect `.env`" in text


def test_ui_skill_stops_when_app_is_down_and_forbids_persistent_actions():
    text = (CODEX_SKILLS / "odi-readonly-ui-smoke" / "SKILL.md").read_text(encoding="utf-8")

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
