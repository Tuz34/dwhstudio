"""Redacted Control Tower evidence summary for local dev/TST status.

This module intentionally avoids live Oracle/ODI connections. It reports the
current orchestration state from local artifacts and known safe workflow gates
so the UI can show "waiting/blocked" explicitly instead of looking inactive.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


SAFE_ARTIFACTS = (
    ("error_pattern_memory", "ODI/ORA error pattern memory", "error_pattern_memory.py"),
    ("reset_safety", "Dev/TST reset dry-run checker", "reset_safety.py"),
    ("morning_report", "Redacted morning report generator", "morning_report.py"),
    ("reset_playbook", "Dev/TST reset playbook", "DEV_TST_RESET_PLAYBOOK.md"),
)


def _artifact(root: Path, artifact_id: str, label: str, rel_path: str) -> dict[str, Any]:
    path = root / rel_path
    return {
        "id": artifact_id,
        "label": label,
        "path": rel_path.replace("\\", "/"),
        "status": "ready" if path.exists() else "missing",
    }


def build_control_tower_evidence(root: str | Path | None = None) -> dict[str, Any]:
    root_path = Path(root or Path(__file__).resolve().parent)
    artifacts = [_artifact(root_path, *item) for item in SAFE_ARTIFACTS]
    ready_artifacts = sum(1 for item in artifacts if item["status"] == "ready")

    chain = [
        {
            "id": "rcu_rebuild",
            "label": "Clean ODI repository rebuild",
            "status": "ready",
            "owner": "User",
            "detail": "REPO rebuild is complete and the current ODI repository is treated as stable/protected.",
            "next_issue": "ODI-39",
        },
        {
            "id": "mapping_inventory",
            "label": "HR/SH mapping inventory",
            "status": "ready",
            "owner": "Codex",
            "detail": "Read-only UI/API smoke can inspect mappings before any scenario metadata write.",
            "next_issue": "ODI-39",
        },
        {
            "id": "scenario_preflight",
            "label": "Scenario metadata preflight",
            "status": "waiting",
            "owner": "Codex",
            "detail": "Next step is ODI-23 read-only readiness check for scenario metadata.",
            "next_issue": "ODI-23",
        },
        {
            "id": "scenario_generation",
            "label": "Scenario metadata generation",
            "status": "waiting",
            "owner": "User / Codex",
            "detail": "Persistent ODI scenario metadata write is not authorized until a separate Evidence Pack and user approval.",
            "next_issue": "ODI-23",
        },
        {
            "id": "post_run_quality",
            "label": "Golden record and DQ gate",
            "status": "waiting",
            "owner": "Codex",
            "detail": "Runs only after a bounded dev/TST ODI scenario or load-plan smoke.",
            "next_issue": "ODI-27",
        },
        {
            "id": "repo_independent_safety",
            "label": "Repo-independent safety tooling",
            "status": "ready" if ready_artifacts >= 3 else "partial",
            "owner": "Codex / Claude Code",
            "detail": "Local report, reset, and error-memory helpers are available for review.",
            "next_issue": "ODI-31/33/34",
        },
    ]

    counts: dict[str, int] = {}
    for item in chain:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready_for_preflight",
        "environment": "local_dev_tst",
        "summary": {
            "total_steps": len(chain),
            "ready_steps": counts.get("ready", 0),
            "blocked_steps": counts.get("blocked", 0),
            "waiting_steps": counts.get("waiting", 0),
            "artifact_ready_count": ready_artifacts,
            "artifact_count": len(artifacts),
        },
        "chain": chain,
        "artifacts": artifacts,
        "safety": {
            "side_effects": False,
            "live_odi_touched": False,
            "oracle_ddl_or_dml": False,
            "raw_rows_included": False,
            "secret_printed": False,
        },
        "notes": [
            "ODI repository rebuild is complete; current mode remains protected/read-only until explicit approval.",
            "Scenario/load-plan run evidence is unavailable until ODI-23/24 create and run approved metadata.",
        ],
    }
