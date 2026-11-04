"""Redacted morning report generator for agent coordination.

The script is local-only and side-effect free by default. It consumes a Linear
issue snapshot JSON file plus CLAUDE.md handoff notes, redacts sensitive text,
and emits a concise morning report. Optional local LLM refinement is blocked if
the raw input contains secret-like content.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import local_llm_analyst


REPORT_SECTIONS = ("Done", "In Progress", "Backlog", "Blockers", "Next agent")
SECRET_REPLACEMENTS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "env_assignment",
        re.compile(r"(?im)^(\s*[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|KEY)\s*=\s*)\S+"),
        r"\1[REDACTED_SECRET]",
    ),
    (
        "assignment_secret",
        re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+"),
        r"\1=[REDACTED_SECRET]",
    ),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_SECRET]"),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_SECRET]"),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        "url_credential",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s]+)@"),
        r"\1[REDACTED_USER]:[REDACTED_SECRET]@",
    ),
    (
        "oracle_easy_connect_credential",
        re.compile(r"(?i)\b([A-Z0-9_]+)/([^@\s]+)@([A-Z0-9_.:-]+)"),
        r"\1/[REDACTED_SECRET]@\3",
    ),
)


@dataclass(frozen=True)
class IssueSnapshot:
    id: str
    title: str
    status: str
    priority: str = ""
    updated_at: str = ""
    comments: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)


def redact_text(text: str) -> tuple[str, list[dict[str, str]]]:
    redacted = text
    findings: list[dict[str, str]] = []
    for name, pattern, replacement in SECRET_REPLACEMENTS:
        redacted, count = pattern.subn(replacement, redacted)
        if count:
            findings.append({"type": name, "action": "redacted", "count": str(count)})
    return redacted, findings


def _safe_string(value: Any) -> str:
    redacted, _ = redact_text(str(value or "").strip())
    return redacted


def _issue_from_mapping(item: dict[str, Any]) -> IssueSnapshot:
    priority = item.get("priority")
    if isinstance(priority, dict):
        priority_text = str(priority.get("name") or priority.get("value") or "")
    else:
        priority_text = str(priority or "")

    comments: list[str] = []
    for comment in item.get("comments") or item.get("recent_comments") or []:
        if isinstance(comment, dict):
            comments.append(_safe_string(comment.get("body") or comment.get("text") or ""))
        else:
            comments.append(_safe_string(comment))

    blocked_by_raw = item.get("blocked_by") or item.get("blockedBy") or []
    if isinstance(blocked_by_raw, dict):
        blocked_by_raw = blocked_by_raw.get("nodes") or blocked_by_raw.get("issues") or []
    blocked_by: list[str] = []
    for blocker in blocked_by_raw:
        if isinstance(blocker, dict):
            blocked_by.append(_safe_string(blocker.get("id") or blocker.get("title") or "unknown"))
        else:
            blocked_by.append(_safe_string(blocker))

    return IssueSnapshot(
        id=_safe_string(item.get("id") or item.get("identifier") or "UNKNOWN"),
        title=_safe_string(item.get("title") or ""),
        status=_safe_string(item.get("status") or item.get("state") or ""),
        priority=_safe_string(priority_text),
        updated_at=_safe_string(item.get("updatedAt") or item.get("updated_at") or ""),
        comments=comments,
        blocked_by=blocked_by,
    )


def parse_linear_snapshot(text: str) -> list[IssueSnapshot]:
    payload = json.loads(text)
    if isinstance(payload, dict):
        records = payload.get("issues") or payload.get("data") or []
    else:
        records = payload
    return [_issue_from_mapping(item) for item in records if isinstance(item, dict)]


def load_linear_snapshot(path: str | Path | None) -> list[IssueSnapshot]:
    if not path:
        return []
    return parse_linear_snapshot(Path(path).read_text(encoding="utf-8-sig"))


def extract_claude_handoffs(text: str, *, limit: int = 8) -> list[str]:
    redacted, _ = redact_text(text)
    blocks: list[str] = []
    current: list[str] = []
    capture = False
    for line in redacted.splitlines():
        if "[HANDOFF]" in line:
            if current:
                blocks.append("\n".join(current).strip())
            current = [line.strip()]
            capture = True
            continue
        if capture:
            if line.startswith("## ") and current:
                blocks.append("\n".join(current).strip())
                current = []
                capture = False
            elif line.strip() == "```" and current:
                blocks.append("\n".join(current).strip())
                current = []
                capture = False
            elif len(current) < 16:
                current.append(line.rstrip())
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block][-limit:]


def _group_issues(issues: list[IssueSnapshot]) -> dict[str, list[IssueSnapshot]]:
    groups = {"Done": [], "In Progress": [], "Backlog": []}
    for issue in issues:
        status = issue.status.lower()
        if status in {"done", "completed"}:
            groups["Done"].append(issue)
        elif status in {"in progress", "started"}:
            groups["In Progress"].append(issue)
        elif status in {"backlog", "todo", "unstarted"}:
            groups["Backlog"].append(issue)
    return groups


def _issue_line(issue: IssueSnapshot) -> str:
    priority = f", {issue.priority}" if issue.priority else ""
    return f"- {issue.id}: {issue.title} ({issue.status}{priority})"


def _find_blockers(issues: list[IssueSnapshot], handoffs: list[str]) -> list[str]:
    blockers: list[str] = []
    blocker_terms = re.compile(r"(?i)\b(blocker|blocked|bekle|waiting|approval|USER APPROVAL NEEDED|RCU|rebuild)\b")
    for issue in issues:
        if issue.blocked_by:
            blockers.append(f"- {issue.id}: blocked by {', '.join(issue.blocked_by)}")
        for comment in issue.comments[-3:]:
            if blocker_terms.search(comment):
                short = " ".join(comment.split())[:220]
                blockers.append(f"- {issue.id}: {short}")
    for block in handoffs[-3:]:
        if blocker_terms.search(block):
            short = " ".join(block.split())[:220]
            blockers.append(f"- Handoff: {short}")
    return blockers[:8]


def choose_next_agent(issues: list[IssueSnapshot], handoffs: list[str]) -> str:
    joined = "\n".join(handoffs[-4:])
    if re.search(r"(?i)To:\s*Claude Code|Claude Code review|Claude review", joined):
        return "Claude Code review queue exists; Codex should continue only repo-independent safe work while waiting."
    if any(issue.status.lower() == "in progress" for issue in issues):
        return "Codex continues the current In Progress issue unless a Claude review handoff appears."
    return "Codex selects the next unblocked, repo-independent Backlog issue."


def build_report(
    issues: list[IssueSnapshot],
    claude_text: str,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now().isoformat(timespec="seconds")
    safe_claude, claude_findings = redact_text(claude_text)
    handoffs = extract_claude_handoffs(safe_claude)
    groups = _group_issues(issues)
    blockers = _find_blockers(issues, handoffs)

    lines = [f"# Morning Agent Report - {generated_at}", ""]
    for section in ("Done", "In Progress", "Backlog"):
        lines.append(f"## {section}")
        selected = groups[section][:8]
        lines.extend(_issue_line(issue) for issue in selected)
        if not selected:
            lines.append("- None")
        lines.append("")

    lines.append("## Blockers")
    lines.extend(blockers or ["- None"])
    lines.append("")

    lines.append("## Recent Handoffs")
    if handoffs:
        for block in handoffs[-5:]:
            first_line = " ".join(block.split())[:260]
            lines.append(f"- {first_line}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Next agent")
    lines.append(f"- {choose_next_agent(issues, handoffs)}")

    report_text, report_findings = redact_text("\n".join(lines).strip() + "\n")
    return {
        "generated_at": generated_at,
        "report": report_text,
        "sections": list(REPORT_SECTIONS),
        "redaction_findings": claude_findings + report_findings,
        "secret_printed": False,
        "local_llm": {"status": "not_requested", "secret_printed": False},
    }


def generate_morning_report(
    *,
    issues: list[IssueSnapshot],
    claude_text: str,
    use_local_llm: bool = False,
    raw_input_text: str = "",
    llm_analyzer: Callable[..., dict[str, Any]] = local_llm_analyst.analyze_text,
) -> dict[str, Any]:
    result = build_report(issues, claude_text)
    if not use_local_llm:
        return result

    raw_input = raw_input_text or (json.dumps([issue.__dict__ for issue in issues], ensure_ascii=True) + "\n" + claude_text)
    blocked = local_llm_analyst.scan_for_blocked_content(raw_input)
    if blocked:
        result["local_llm"] = {
            "status": "blocked_input",
            "secret_printed": False,
            "findings": blocked,
            "message": "Raw input contains secret-like content; local model call was not attempted.",
        }
        return result

    llm_result = llm_analyzer(
        result["report"],
        task="Produce a concise Turkish morning agent report with done, blockers, and next action.",
        auto_load=True,
        unload_after=True,
    )
    result["local_llm"] = {
        "status": llm_result.get("status"),
        "secret_printed": False,
        "loaded_by_helper": llm_result.get("loaded_by_helper"),
        "unload_after": llm_result.get("unload_after"),
        "analysis": _safe_string(llm_result.get("analysis", "")),
    }
    if result["local_llm"].get("analysis"):
        result["report"] = str(result["local_llm"]["analysis"]).strip() + "\n"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a redacted morning agent report.")
    parser.add_argument("--linear-json", help="Linear issue snapshot JSON file.")
    parser.add_argument("--claude", default="CLAUDE.md", help="CLAUDE.md path.")
    parser.add_argument("--use-local-llm", action="store_true", help="Refine with local LLM after raw-input safety scan.")
    parser.add_argument("--out", help="Optional output markdown path.")
    args = parser.parse_args()

    linear_raw = Path(args.linear_json).read_text(encoding="utf-8-sig") if args.linear_json else ""
    issues = parse_linear_snapshot(linear_raw) if linear_raw else []
    claude_text = Path(args.claude).read_text(encoding="utf-8", errors="ignore") if args.claude else ""
    result = generate_morning_report(
        issues=issues,
        claude_text=claude_text,
        use_local_llm=args.use_local_llm,
        raw_input_text=linear_raw + "\n" + claude_text,
    )
    if args.out:
        Path(args.out).write_text(result["report"], encoding="utf-8")
    print(result["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
