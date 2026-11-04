from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from command_safety import ENFORCEMENT_MODE, classify_command


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "command",
    [
        r".\.venv\Scripts\python.exe -m pytest -q",
        r".\.venv\Scripts\python.exe python_quality_gate.py",
        r"python D:\Proje\tools\workspace\appsec_gate.py --no-write",
        "npm test",
        "npm run lint",
        "npm run build",
        "rg -n approval routes tests",
        'rg -n "DROP TABLE" routes tests',
        "Get-Content odi_automation/run_scenario.groovy",
        "Select-String -Path docs/*.md -Pattern docker",
        "Get-Content setup_oracle_users.py",
        "Get-Content docs/groovy-execution-boundary.md",
        "Get-Content .env.example",
    ],
)
def test_normal_quality_and_metadata_commands_are_allowed(command):
    report = classify_command(command)

    assert report.mode == "report_only"
    assert report.would_block is False
    assert report.findings == ()


@pytest.mark.parametrize(
    ("command", "rule_id"),
    [
        ("git status", "version_control"),
        ("gitea admin user list", "version_control"),
        ("Get-Content .env", "sensitive_file_read"),
        ("rg password odi_automation/odi_connection.properties", "sensitive_file_read"),
        ("sqlplus -S @maintenance.sql", "persistent_database"),
        ('sqlcmd -Q "TRUNCATE TABLE DWH.X"', "persistent_database"),
        ("python setup_oracle_users.py", "persistent_database"),
        ("groovy odi_automation/run_scenario.groovy", "live_odi_groovy"),
        ("python odi_automation/full_odi_run.py", "live_odi_groovy"),
        ("lms load qwen", "model_runtime"),
        ("python local_model_eval.py --capture", "model_runtime"),
        ("Register-ScheduledTask -TaskName ODI", "persistent_infrastructure"),
        ("powershell ./register_task.ps1 -Arm", "persistent_infrastructure"),
        ("docker compose up -d", "persistent_infrastructure"),
        ("StartWebLogic.cmd", "persistent_infrastructure"),
    ],
)
def test_high_risk_commands_are_reported_without_enforcement(command, rule_id):
    report = classify_command(command)

    assert report.mode == ENFORCEMENT_MODE == "report_only"
    assert report.would_block is True
    assert rule_id in {finding.rule_id for finding in report.findings}
    assert report.command_included is False


def test_cli_never_echoes_sensitive_command_and_remains_report_only():
    sensitive_marker = "do-not-echo-this-value"
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "command_safety.py")],
        input=f"Get-Content .env # {sensitive_marker}",
        text=True,
        capture_output=True,
        check=False,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["mode"] == "report_only"
    assert payload["would_block"] is True
    assert payload["command_included"] is False
    assert sensitive_marker not in result.stdout


def test_cli_exposes_no_blocking_mode():
    source = (PROJECT_ROOT / "command_safety.py").read_text(encoding="utf-8")

    assert "argparse" not in source
    assert "--blocking" not in source
    assert "ENFORCEMENT_MODE = \"report_only\"" in source
