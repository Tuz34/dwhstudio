import json

import morning_report


def test_redacts_secret_like_text_from_report():
    issues = [
        morning_report.IssueSnapshot(
            id="ODI-99",
            title="Fix local report PASSWORD=secret",
            status="Done",
            comments=["ORACLE_PASSWORD=secret should not leak"],
        )
    ]

    result = morning_report.generate_morning_report(
        issues=issues,
        claude_text="[HANDOFF] To: Claude Code\npassword: secret\n",
    )

    assert result["secret_printed"] is False
    assert "password: secret" not in result["report"].lower()
    assert "PASSWORD=secret" not in result["report"]
    assert "[REDACTED_SECRET]" in result["report"]


def test_report_has_required_sections_and_next_agent():
    issues = [
        morning_report.IssueSnapshot(id="ODI-1", title="Finished work", status="Done"),
        morning_report.IssueSnapshot(id="ODI-2", title="Current work", status="In Progress"),
        morning_report.IssueSnapshot(id="ODI-3", title="Future work", status="Backlog"),
    ]

    result = morning_report.build_report(
        issues,
        "[HANDOFF] 2026-06-06\n- To: Claude Code\n- Done: review needed\n",
        generated_at="2026-06-06T16:00:00",
    )

    report = result["report"]
    assert "## Done" in report
    assert "## In Progress" in report
    assert "## Backlog" in report
    assert "## Blockers" in report
    assert "## Next agent" in report
    assert "Claude Code review queue exists" in report


def test_blockers_include_blocked_issue_and_handoff_waiting_text():
    issues = [
        morning_report.IssueSnapshot(
            id="ODI-36",
            title="Mapping rebuild",
            status="Backlog",
            blocked_by=["RCU rebuild"],
        )
    ]

    result = morning_report.build_report(
        issues,
        "[HANDOFF] To: Codex\n- Blocker: waiting for clean RCU rebuild\n",
    )

    assert "ODI-36: blocked by RCU rebuild" in result["report"]
    assert "waiting for clean RCU rebuild" in result["report"]


def test_local_llm_is_not_called_when_raw_input_contains_secret():
    calls = []
    issues = [
        morning_report.IssueSnapshot(
            id="ODI-10",
            title="Safe title",
            status="Done",
            comments=["TOKEN=abc123"],
        )
    ]

    def fake_analyzer(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "ready", "analysis": "should not run"}

    result = morning_report.generate_morning_report(
        issues=issues,
        claude_text="clean handoff",
        use_local_llm=True,
        llm_analyzer=fake_analyzer,
    )

    assert result["local_llm"]["status"] == "blocked_input"
    assert result["local_llm"]["secret_printed"] is False
    assert calls == []


def test_clean_local_llm_call_requests_unload_after():
    calls = []

    def fake_analyzer(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "status": "ready",
            "loaded_by_helper": True,
            "unload_after": True,
            "analysis": "Kisa sabah ozeti\n- Sonraki adim Codex",
        }

    result = morning_report.generate_morning_report(
        issues=[morning_report.IssueSnapshot(id="ODI-33", title="Morning report", status="In Progress")],
        claude_text="[HANDOFF] To: Codex\n",
        use_local_llm=True,
        llm_analyzer=fake_analyzer,
    )

    assert result["local_llm"]["status"] == "ready"
    assert result["local_llm"]["loaded_by_helper"] is True
    assert result["local_llm"]["unload_after"] is True
    assert calls[0][1]["auto_load"] is True
    assert calls[0][1]["unload_after"] is True


def test_raw_input_text_blocks_local_llm_even_if_snapshot_is_redacted():
    calls = []

    result = morning_report.generate_morning_report(
        issues=[morning_report.IssueSnapshot(id="ODI-1", title="Safe", status="Done")],
        claude_text="clean",
        use_local_llm=True,
        raw_input_text='{"issues":[{"id":"ODI-1","comments":["PASSWORD=secret"]}]}',
        llm_analyzer=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert result["local_llm"]["status"] == "blocked_input"
    assert calls == []


def test_load_linear_snapshot_accepts_bom_json(tmp_path):
    path = tmp_path / "linear.json"
    path.write_text(
        "\ufeff" + json.dumps({"issues": [{"id": "ODI-1", "title": "One", "status": "Done"}]}),
        encoding="utf-8",
    )

    issues = morning_report.load_linear_snapshot(path)

    assert issues[0].id == "ODI-1"
    assert issues[0].status == "Done"
