"""Dry-run reset safety checks for local dev/TST ODI/ODS experiments.

The checker does not connect to Oracle or ODI. It validates proposed reset
targets and inventories local scripts so destructive work is explicit before
any live action is attempted.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RESET_LEVELS = {
    "metadata_only": "ODI repository metadata only; no Oracle table DDL/DML.",
    "ods_tables_only": "DWH-owned ODS/STG/TMP/DQ/SKEY objects only.",
    "full_dev_tst_reseed": "Full dev/TST rebuild; always requires explicit user scope.",
}

METADATA_ACTIONS = {
    "DELETE_MAPPING",
    "DELETE_DATASTORE",
    "SEED_PROJECT",
    "SEED_MODEL",
    "REPAIR_METADATA",
    "GENERATE_SCENARIO_METADATA",
}
ODS_ACTIONS = {"CREATE", "DROP", "TRUNCATE", "DELETE", "ALTER", "RECREATE"}
ALLOWED_DWH_OBJECT_RE = re.compile(r"^(ODS_|STG_|TMP_|DQ_|SKEY_)[A-Z0-9_#$]+$")
FORBIDDEN_OWNER_RE = re.compile(
    r"^(SYS|SYSTEM|HR|SH|AUDIT_USER|"
    r"(DEV|ODIREPO|REPO)_(ODI_REPO|STB|MDS|IAU|IAU_APPEND|IAU_VIEWER|OPSS|UMS|WLS|WLS_RUNTIME)|"
    r"ODI_REPO)$"
)
SECRET_FILE_RE = re.compile(r"(?i)(^|[\\/])\.env($|[._-])|odi_connection\.properties$")
DESTRUCTIVE_TOKEN_RE = re.compile(
    r"(?i)\b(DROP\s+USER|DROP\s+TABLE|TRUNCATE\s+TABLE|DELETE\s+FROM|em\.remove|CASCADE\s+CONSTRAINTS?)\b"
)


@dataclass(frozen=True)
class ResetTarget:
    action: str
    object_ref: str
    source: str = "manual"


def _parse_owner_object(object_ref: str) -> tuple[str, str] | None:
    ref = object_ref.strip().upper()
    if "." not in ref or ref.startswith("ODI:"):
        return None
    owner, name = ref.split(".", 1)
    if not owner or not name or "." in name:
        return None
    return owner, name


def validate_reset_target(
    target: ResetTarget,
    *,
    level: str,
    explicit_scope: bool = False,
) -> dict[str, Any]:
    if level not in RESET_LEVELS:
        return {"status": "blocked", "reason": "unknown_reset_level", "target": target.__dict__}

    action = target.action.strip().upper()
    object_ref = target.object_ref.strip()
    ref_upper = object_ref.upper()

    if level == "full_dev_tst_reseed":
        return {
            "status": "allowed" if explicit_scope else "blocked",
            "reason": "explicit_scope_required" if not explicit_scope else "explicit_full_reseed_scope",
            "target": target.__dict__,
        }

    if level == "metadata_only":
        if not ref_upper.startswith("ODI:"):
            return {"status": "blocked", "reason": "metadata_only_rejects_oracle_object", "target": target.__dict__}
        if action not in METADATA_ACTIONS:
            return {"status": "blocked", "reason": "metadata_action_not_allowed", "target": target.__dict__}
        return {"status": "allowed", "reason": "odi_metadata_allowlist", "target": target.__dict__}

    owner_object = _parse_owner_object(object_ref)
    if owner_object is None:
        return {"status": "blocked", "reason": "oracle_owner_object_required", "target": target.__dict__}
    owner, name = owner_object

    if action not in ODS_ACTIONS:
        return {"status": "blocked", "reason": "ods_action_not_allowed", "target": target.__dict__}
    if FORBIDDEN_OWNER_RE.match(owner):
        return {"status": "blocked", "reason": "owner_forbidden_for_ods_reset", "target": target.__dict__}
    if owner != "DWH":
        return {"status": "blocked", "reason": "owner_not_dwh", "target": target.__dict__}
    if not ALLOWED_DWH_OBJECT_RE.match(name):
        return {"status": "blocked", "reason": "object_not_in_dev_tst_allowlist", "target": target.__dict__}
    return {"status": "allowed", "reason": "dwh_dev_tst_object_allowlist", "target": target.__dict__}


def build_dry_run_report(
    targets: list[ResetTarget],
    *,
    level: str,
    explicit_scope: bool = False,
) -> dict[str, Any]:
    checks = [
        validate_reset_target(target, level=level, explicit_scope=explicit_scope)
        for target in targets
    ]
    blocked = [check for check in checks if check["status"] == "blocked"]
    return {
        "level": level,
        "status": "blocked" if blocked else "allowed",
        "side_effects": False,
        "checks": checks,
        "blocked_count": len(blocked),
        "allowed_count": len(checks) - len(blocked),
    }


def inventory_scripts(root: str | Path) -> list[dict[str, Any]]:
    root_path = Path(root)
    candidates = list(root_path.glob("*.py")) + list((root_path / "odi_automation").glob("*.groovy"))
    inventory: list[dict[str, Any]] = []
    for path in sorted(candidates):
        rel = str(path.relative_to(root_path))
        if SECRET_FILE_RE.search(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        destructive = sorted(set(match.group(0).upper() for match in DESTRUCTIVE_TOKEN_RE.finditer(text)))
        if destructive:
            inventory.append(
                {
                    "path": rel.replace("\\", "/"),
                    "destructive_tokens": destructive,
                    "review_required": True,
                }
            )
    return inventory


def _target_from_json(item: dict[str, Any]) -> ResetTarget:
    return ResetTarget(
        action=str(item.get("action", "")),
        object_ref=str(item.get("object_ref", "")),
        source=str(item.get("source", "manual")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run reset safety checker.")
    parser.add_argument("--level", choices=sorted(RESET_LEVELS), default="metadata_only")
    parser.add_argument("--targets-json", help="JSON array of {action, object_ref, source}.")
    parser.add_argument("--targets-file", help="Path to JSON array of {action, object_ref, source}.")
    parser.add_argument("--inventory", action="store_true", help="Inventory local destructive scripts.")
    parser.add_argument("--root", default=".", help="Workspace root for inventory mode.")
    parser.add_argument("--explicit-scope", action="store_true", help="Mark full reseed as explicitly scoped.")
    args = parser.parse_args()

    output: dict[str, Any] = {}
    if args.inventory:
        output["inventory"] = inventory_scripts(args.root)
    target_payload = None
    if args.targets_file:
        target_payload = Path(args.targets_file).read_text(encoding="utf-8-sig")
    elif args.targets_json:
        target_payload = args.targets_json
    if target_payload:
        targets = [_target_from_json(item) for item in json.loads(target_payload)]
        output["dry_run"] = build_dry_run_report(
            targets,
            level=args.level,
            explicit_scope=args.explicit_scope,
        )
    if not output:
        parser.print_help()
        return 0
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
