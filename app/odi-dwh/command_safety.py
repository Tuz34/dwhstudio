from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass

ENFORCEMENT_MODE = "report_only"


@dataclass(frozen=True)
class SafetyFinding:
    rule_id: str
    severity: str
    action: str


@dataclass(frozen=True)
class SafetyReport:
    mode: str
    would_block: bool
    command_included: bool
    findings: tuple[SafetyFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "would_block": self.would_block,
            "command_included": self.command_included,
            "findings": [asdict(finding) for finding in self.findings],
        }


_READ_TOOL = re.compile(r"^\s*(?:get-content|gc|cat|type|more|select-string|sls|findstr|rg)\b", re.IGNORECASE)
_SENSITIVE_PATH = re.compile(
    r"(?:^|[\s'\"=/\\])(?:\.env(?!\.example\b)(?:\.[\w.-]+)?|odi_connection\.properties(?:\.[\w.-]+)?)\b",
    re.IGNORECASE,
)


def _matches(pattern: str, command: str) -> bool:
    return re.search(pattern, command, flags=re.IGNORECASE) is not None


def classify_command(command: str) -> SafetyReport:
    """Classify a command without executing it or returning its contents."""
    findings: list[SafetyFinding] = []
    stripped = command.strip()
    inspection_command = _READ_TOOL.search(stripped) is not None

    if _matches(r"^(?:&\s*)?(?:git|gitea)(?:\.exe)?\b", stripped):
        findings.append(SafetyFinding("version_control", "high", "requires_explicit_review"))

    if _READ_TOOL.search(stripped) and _SENSITIVE_PATH.search(stripped):
        findings.append(SafetyFinding("sensitive_file_read", "critical", "do_not_inspect"))

    db_client = _matches(r"^(?:&\s*)?(?:sqlplus|sql|sqlcl|sqlcmd)(?:\.exe)?\b|\binvoke-sqlcmd\b", stripped)
    destructive_sql = _matches(r"\b(?:drop|truncate)\s+(?:table|schema|database|user)\b", stripped)
    persistent_db_script = _matches(
        r"\b(?:setup_oracle_users|init_audit_infra|reconciliation_autofix)\.py\b", stripped
    )
    if db_client or (not inspection_command and (destructive_sql or persistent_db_script)):
        findings.append(SafetyFinding("persistent_database", "critical", "require_backup_and_exact_approval"))

    live_groovy = _matches(r"^(?:&\s*)?groovy(?:\.exe)?\b", stripped) or _matches(
        r"^(?:&\s*)?java(?:\.exe)?\b.*(?:\bgroovy\b|\bodi_automation[\\/].*\.groovy\b)", stripped
    )
    live_groovy = live_groovy or (not inspection_command and _matches(r"\bfull_odi_run\.py\b", stripped))
    if live_groovy:
        findings.append(SafetyFinding("live_odi_groovy", "critical", "require_preflight_backup_and_exact_approval"))

    model_action = _matches(
        r"\b(?:lms|lmstudio)\s+(?:load|unload|download)\b|\bollama\s+(?:pull|run|rm|stop)\b|"
        r"\bhuggingface-cli\s+download\b|\blocal_model_eval\.py\b.*(?:--capture|--runtime-probe)",
        stripped,
    )
    if model_action and not inspection_command:
        findings.append(SafetyFinding("model_runtime", "high", "require_exact_approval"))

    infrastructure_action = _matches(
        r"\b(?:register-scheduledtask|unregister-scheduledtask|schtasks)(?:\.exe)?\b|"
        r"\bregister_task\.ps1\b.*\s-arm\b|"
        r"\bdocker(?:\.exe)?\s+(?:run|start|stop|rm|compose\s+up|compose\s+down)\b|"
        r"\b(?:startweblogic|stopweblogic)(?:\.cmd|\.sh)?\b",
        stripped,
    )
    if infrastructure_action and not inspection_command:
        findings.append(SafetyFinding("persistent_infrastructure", "critical", "require_backup_and_exact_approval"))

    unique = tuple(dict.fromkeys(findings))
    return SafetyReport(
        mode=ENFORCEMENT_MODE,
        would_block=bool(unique),
        command_included=False,
        findings=unique,
    )


def main() -> int:
    command = sys.stdin.read()
    report = classify_command(command)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
