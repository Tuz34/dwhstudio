"""Fixed DIM_PRODUCT audit-role preflight, repair and restore boundary.

Changes only the ODI SCD role of four audit columns on the fixed datastore. It touches
no database row, no other column and no scenario. A repaired datastore makes any existing
scenario stale until a separately reviewed new version exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from dim_product_odi_scd2_packaging_readiness import MAPPING_NAME, SOURCE, TARGET
from dim_product_scd2_scenario_backup import call_sanitized
from dim_product_scd2_stock_contract import (
    MAPPING_ETL_BATCH_ID_EXPRESSION,
    MAPPING_INSERT_DATE_EXPRESSION,
    MAPPING_SOURCE_SYSTEM_EXPRESSION,
    MAPPING_UPDATE_DATE_EXPRESSION,
)
from scd2_builder import TARGET_MODEL_BY_OWNER
from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

TOOL = "odi.dim_product_audit_role_repair"
RAW_MODE = "fixed_dim_product_audit_role_repair"
NORMALIZED_MODE = "normalized_fixed_dim_product_audit_role_repair"
SCRIPT_NAME = "dim_product_audit_role_repair.groovy"
PREFLIGHT_SCOPE = "odi87_dim_product_audit_role_preflight"
REPAIR_SCOPE = "odi87_dim_product_audit_role_repair"
RESTORE_SCOPE = "odi87_dim_product_audit_role_restore"
RECEIPT_NAME = "odi87_dim_product_audit_role_receipt.json"
OUTCOME_NAME = "odi87_dim_product_audit_role_outcome.json"
RESTORE_NAME = "odi87_dim_product_audit_role_restore.json"

AUDIT_COLUMNS = ("INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID")
NULL_ROLE = "NULL"
OVERWRITE_ROLE = "OVERWRITE_ON_CHANGE"
_ROLE_VALUES = frozenset({NULL_ROLE, OVERWRITE_ROLE})
_SCOPES = {"preflight": PREFLIGHT_SCOPE, "repair": REPAIR_SCOPE, "restore": RESTORE_SCOPE}
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,127}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
_CODES = frozenset(
    {
        "invalid_input",
        "project_contract",
        "model_contract",
        "datastore_contract",
        "mapping_contract",
        "column_contract",
        "role_contract",
        "audit_role_unexpected",
        "audit_expression_drift",
        "before_image_drift",
        "restore_state_drift",
        "active_odi_sessions",
        "post_write_verification",
    }
)
_FLAGS = (
    "commit_attempted",
    "commit_acknowledged",
    "rollback_attempted",
    "rollback_succeeded",
    "manual_recovery_review_required",
)
_RAW_KEYS = frozenset(
    {
        "tool",
        "mode",
        "action",
        "status",
        "code",
        "error_type",
        "side_effects",
        "repository_connection_attempted",
        "raw_rows_included",
        "pk_values_included",
        "secret_printed",
        "repository_outcome",
        "write_attempted",
        "scenario_regeneration_required",
        "active_session_count",
        "before_image",
        "after_image",
        "changed_columns",
        *_FLAGS,
    }
)
# (status, repository_outcome): (side_effects, then _FLAGS in order). A flag of None accepts
# either boolean. side_effects None means the repository state is unknown.
_STATES: dict[tuple[str, str], tuple[Any, ...]] = {
    ("ready_for_repair", "no_write"): (False, False, False, True, True, False),
    ("already_repaired", "no_write"): (False, False, False, True, True, False),
    ("already_restored", "no_write"): (False, False, False, True, True, False),
    ("repaired", "committed"): (True, True, True, False, False, False),
    ("restored", "committed"): (True, True, True, False, False, False),
    ("blocked", "rolled_back"): (False, False, False, True, True, False),
    ("blocked", "no_transaction"): (False, False, False, False, False, False),
    ("aborted", "rolled_back"): (False, False, False, True, True, False),
    ("aborted", "no_transaction"): (False, False, False, False, False, False),
    ("outcome_unknown", "rollback_failed"): (None, False, False, True, False, True),
    ("outcome_unknown", "commit_outcome_unknown"): (None, True, False, True, None, True),
    ("committed_report_incomplete", "committed"): (True, True, True, False, False, True),
}
_NO_EFFECT_OUTCOMES = frozenset({"rolled_back", "no_transaction", "no_write"})
_RECEIPT_FIELDS = (
    "schema_version",
    "tool",
    "scope",
    "model",
    "mapping",
    "source",
    "target",
    "before_image",
    "target_image",
    "changed_columns",
)
RECEIPT_KEYS = frozenset({*_RECEIPT_FIELDS, "digest"})
_OUTCOME_KEYS = frozenset({"schema_version", "action", "status", "repository_outcome", "side_effects", "digest"})


def expected_contract() -> dict[str, Any]:
    """The frozen column and expression contract, taken from the shared fixed sources."""
    plan = build_dim_product_scd2_plan(
        [
            {"name": name, "data_type": data_type, "nullable": nullable}
            for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
        ]
    )
    return {
        "model_code": TARGET_MODEL_BY_OWNER[TARGET["owner"]],
        "mapping": MAPPING_NAME,
        "source_table": SOURCE["table"],
        "target_table": TARGET["table"],
        "target_columns": [column["name"] for column in plan["target_columns"]],
        "source_columns": [*plan["tracked_attributes"], *plan["source_columns_excluded_from_change_detection"]],
        "tracked": list(plan["tracked_attributes"]),
        "surrogate_key": plan["surrogate_key"]["column"],
        "natural_key": plan["natural_key"][0],
        "start_date": "EFFECTIVE_FROM",
        "end_date": "EFFECTIVE_TO",
        "current_flag": "CURRENT_FLAG",
        "audit_expressions": {
            "INSERT_DATE": MAPPING_INSERT_DATE_EXPRESSION,
            "UPDATE_DATE": MAPPING_UPDATE_DATE_EXPRESSION,
            "SOURCE_SYSTEM": MAPPING_SOURCE_SYSTEM_EXPRESSION,
            "ETL_BATCH_ID": MAPPING_ETL_BATCH_ID_EXPRESSION,
        },
    }


def encode_image(image: Mapping[str, str]) -> str:
    """Encode a role image for the fixed script. Names and roles only."""
    return ",".join(f"{column}={image[column]}" for column in AUDIT_COLUMNS)


def _base(action: object, status: str) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "mode": NORMALIZED_MODE,
        "action": action if isinstance(action, str) and action in _SCOPES else None,
        "status": status,
        "side_effects": False,
        "repository_connection_attempted": False,
        "manual_recovery_review_required": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _unverified(action: object, status: str, error_type: str = "InvalidAuditRoleOutput") -> dict[str, Any]:
    """Output that cannot be trusted proves nothing about the repository."""
    return {
        **_base(action, status),
        "side_effects": None,
        "repository_connection_attempted": True,
        "manual_recovery_review_required": True,
        "repository_outcome": "unverified",
        "scenario_regeneration_required": True,
        "error_type": error_type,
    }


def _invalid(action: object) -> dict[str, Any]:
    return _unverified(action, "blocked_invalid_output")


def _image_valid(image: object) -> bool:
    return (
        isinstance(image, dict)
        and set(image) == set(AUDIT_COLUMNS)
        and all(isinstance(value, str) and value in _ROLE_VALUES for value in image.values())
    )


def _images_consistent(payload: Mapping[str, Any]) -> bool:
    status = payload["status"]
    before, after, changed = payload["before_image"], payload["after_image"], payload["changed_columns"]
    if not isinstance(changed, list) or any(column not in AUDIT_COLUMNS for column in changed):
        return False
    if len(changed) != len(set(changed)):
        return False
    if before is not None and not _image_valid(before):
        return False
    if after is not None and not _image_valid(after):
        return False
    if status == "ready_for_repair":
        return before is not None and NULL_ROLE in before.values() and after is None and not changed
    if status == "already_repaired":
        return before is not None and set(before.values()) == {OVERWRITE_ROLE} and after is None and not changed
    if status == "already_restored":
        return before is not None and after is None and not changed
    if status in {"repaired", "restored", "committed_report_incomplete"}:
        if before is None or after is None or not changed:
            return False
        from_role, to_role = (
            (NULL_ROLE, OVERWRITE_ROLE) if payload["action"] == "repair" else (OVERWRITE_ROLE, NULL_ROLE)
        )
        return all(before[column] == from_role and after[column] == to_role for column in changed) and all(
            before[column] == after[column] for column in AUDIT_COLUMNS if column not in changed
        )
    return True


def _status_allowed_for_action(status: str, action: str) -> bool:
    allowed = {
        "ready_for_repair": {"preflight"},
        "already_repaired": {"preflight", "repair"},
        "already_restored": {"restore"},
        "repaired": {"repair"},
        "restored": {"restore"},
        "committed_report_incomplete": {"repair", "restore"},
    }
    return status not in allowed or action in allowed[status]


def normalize_audit_role_report(payload: object, action: object) -> dict[str, Any]:
    """Accept only exact truthful report states. Anything else stays unverified."""
    if not isinstance(payload, Mapping) or not isinstance(action, str) or action not in _SCOPES:
        return _invalid(action)
    try:
        status, outcome = payload.get("status"), payload.get("repository_outcome")
        expected = _STATES.get((status, outcome)) if isinstance(status, str) and isinstance(outcome, str) else None
    except TypeError:
        return _invalid(action)
    if expected is None or set(payload) != _RAW_KEYS:
        return _invalid(action)
    side_effects, *flags = expected
    code, error_type = payload["code"], payload["error_type"]
    count = payload["active_session_count"]
    regeneration = payload["scenario_regeneration_required"]
    if (
        payload["tool"] != TOOL
        or payload["mode"] != RAW_MODE
        or payload["action"] != action
        or not _status_allowed_for_action(status, action)
        or payload["side_effects"] is not side_effects
        or type(payload["repository_connection_attempted"]) is not bool
        or (payload["repository_connection_attempted"] is False and outcome != "no_transaction")
        or payload["raw_rows_included"] is not False
        or payload["pk_values_included"] is not False
        or payload["secret_printed"] is not False
        or type(payload["write_attempted"]) is not bool
        or type(regeneration) is not bool
        or (count is not None and (type(count) is not int or count < 0))
        or (code is not None and (not isinstance(code, str) or code not in _CODES))
        or (error_type is not None and (not isinstance(error_type, str) or not _ERROR_TYPE_RE.fullmatch(error_type)))
    ):
        return _invalid(action)
    for name, flag in zip(_FLAGS, flags, strict=True):
        value = payload[name]
        if type(value) is not bool or (flag is not None and value is not flag):
            return _invalid(action)
    if (
        (status == "blocked" and code is None)
        or (status not in {"blocked", "outcome_unknown"} and code is not None)
        or (status in {"aborted", "committed_report_incomplete"} and error_type is None)
        or (outcome == "commit_outcome_unknown" and error_type is None)
        or (status in {"ready_for_repair", "already_repaired", "already_restored", "repaired", "restored", "blocked"} and error_type is not None)
        or (outcome == "commit_outcome_unknown" and regeneration is not True)
        or (outcome == "committed" and regeneration is not True)
        or (outcome in _NO_EFFECT_OUTCOMES and regeneration is not False)
        or (status in {"repaired", "restored"} and payload["write_attempted"] is not True)
        or (outcome == "no_write" and payload["write_attempted"] is not False)
        or not _images_consistent(payload)
    ):
        return _invalid(action)
    return {
        **_base(action, status),
        "side_effects": side_effects,
        "repository_connection_attempted": payload["repository_connection_attempted"],
        "repository_outcome": outcome,
        "code": code,
        "error_type": error_type,
        "write_attempted": payload["write_attempted"],
        "scenario_regeneration_required": regeneration,
        "active_session_count": count,
        "before_image": payload["before_image"],
        "after_image": payload["after_image"],
        "changed_columns": list(payload["changed_columns"]),
        **{name: payload[name] for name in _FLAGS},
    }


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate")
    return dict(pairs)


def _parse(text: object) -> object:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text.strip(), object_pairs_hook=_no_duplicates)
    except ValueError:
        return None


def _digest(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_absent(path: Path, payload: Mapping[str, Any]) -> bool:
    """Write one new file. An existing file is never replaced."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({**payload, "digest": _digest(payload)}, handle, sort_keys=True)
    except OSError:
        return False
    return True


def _new_receipt(before: Mapping[str, str]) -> dict[str, Any]:
    changed = [column for column in AUDIT_COLUMNS if before[column] == NULL_ROLE]
    return {
        "schema_version": 1,
        "tool": TOOL,
        "scope": REPAIR_SCOPE,
        "model": TARGET_MODEL_BY_OWNER[TARGET["owner"]],
        "mapping": MAPPING_NAME,
        "source": SOURCE["table"],
        "target": TARGET["table"],
        "before_image": dict(before),
        "target_image": {column: OVERWRITE_ROLE for column in AUDIT_COLUMNS},
        "changed_columns": changed,
    }


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        receipt = _parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS or receipt.get("digest") != _digest(receipt):
        return None
    expected = _new_receipt(receipt["before_image"]) if _image_valid(receipt["before_image"]) else None
    if expected is None or any(receipt[key] != expected[key] for key in _RECEIPT_FIELDS) or not receipt["changed_columns"]:
        return None
    return receipt


def _read_outcome(path: Path) -> str | None:
    """Return the recorded outcome. Missing or invalid evidence proves nothing."""
    try:
        outcome = _parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    if not isinstance(outcome, dict) or set(outcome) != _OUTCOME_KEYS or outcome.get("digest") != _digest(outcome):
        return None
    value = outcome["repository_outcome"]
    return value if isinstance(value, str) else None


def _outcome_record(report: Mapping[str, Any], action: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "action": action,
        "status": report["status"],
        "repository_outcome": report["repository_outcome"],
        "side_effects": report["side_effects"],
    }


def _script_parameters(action: str, scope: str, project: str, folder: str, **extra: str) -> dict[str, str]:
    return {
        "odi.audit.action": action,
        "odi.audit.approved": "true",
        "odi.audit.approval_scope": scope,
        "odi.project": project,
        "odi.folder": folder,
        **extra,
    }


def _preflight(run_script: Callable[[dict[str, str]], Any], project: str, folder: str) -> dict[str, Any]:
    output = call_sanitized(run_script, _script_parameters("preflight", PREFLIGHT_SCOPE, project, folder))
    if isinstance(output, str) and output.startswith("Hata: ODI Groovy action blocked"):
        return {**_base("preflight", "blocked_not_launched")}
    report = normalize_audit_role_report(_parse(output), "preflight")
    if report["status"] == "blocked_invalid_output":
        # The read-only pass has no write path, so an unusable report here cannot mean a change.
        return {**report, "side_effects": False, "manual_recovery_review_required": False, "scenario_regeneration_required": False}
    return report


def _writer_report(action: str, output: object) -> dict[str, Any]:
    """A write call that gave no trustworthy report may still have committed."""
    if output is None:
        return _unverified(action, "outcome_unknown", "ScriptCallbackFailed")
    if isinstance(output, str) and output.startswith("Hata: ODI Groovy action blocked"):
        return {**_base(action, "blocked_not_launched")}
    report = normalize_audit_role_report(_parse(output), action)
    if report["status"] == "blocked_invalid_output":
        return _unverified(action, "outcome_unknown")
    return report


def _keep_commit_if_record_failed(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("repository_outcome") == "committed":
        return {**report, "status": "committed_report_incomplete", "manual_recovery_review_required": True}
    return {**report, "manual_recovery_review_required": True}


def _repair(run_script: Callable[[dict[str, str]], Any], directory: Path, project: str, folder: str) -> dict[str, Any]:
    receipt_path = directory / RECEIPT_NAME
    if receipt_path.exists():
        return _base("repair", "blocked_receipt_exists")
    fresh = _preflight(run_script, project, folder)
    if fresh["status"] != "ready_for_repair":
        return {**fresh, "action": "repair"}
    before = fresh["before_image"]
    if not _write_absent(receipt_path, _new_receipt(before)):
        return {**_base("repair", "blocked_receipt_unwritable"), "repository_connection_attempted": True}
    output = call_sanitized(
        run_script,
        _script_parameters("repair", REPAIR_SCOPE, project, folder, **{"odi.audit.expected_before": encode_image(before)}),
    )
    report = {**_writer_report("repair", output), "receipt_written": True}
    if report["status"] == "blocked_not_launched":
        return report
    if not _write_absent(directory / OUTCOME_NAME, _outcome_record(report, "repair")):
        return _keep_commit_if_record_failed(report)
    return report


def _restore(run_script: Callable[[dict[str, str]], Any], directory: Path, project: str, folder: str) -> dict[str, Any]:
    receipt_path = directory / RECEIPT_NAME
    if not receipt_path.exists():
        return _base("restore", "blocked_receipt_missing")
    receipt = _read_receipt(receipt_path)
    if receipt is None:
        return _base("restore", "blocked_receipt_invalid")
    marker = directory / RESTORE_NAME
    if marker.exists():
        return _base("restore", "blocked_receipt_stale")
    if _read_outcome(directory / OUTCOME_NAME) in _NO_EFFECT_OUTCOMES:
        return _base("restore", "blocked_receipt_not_applied")
    output = call_sanitized(
        run_script,
        _script_parameters(
            "restore",
            RESTORE_SCOPE,
            project,
            folder,
            **{
                "odi.audit.expected_before": encode_image(receipt["before_image"]),
                "odi.audit.changed_columns": ",".join(receipt["changed_columns"]),
            },
        ),
    )
    report = _writer_report("restore", output)
    if report["status"] in {"blocked", "aborted", "blocked_not_launched"}:
        return report
    if not _write_absent(marker, _outcome_record(report, "restore")):
        return _keep_commit_if_record_failed(report)
    return report


def run_audit_role_action(
    *,
    action: object,
    approved: object,
    approval_scope: object,
    implementation_review_status: object,
    no_active_odi_session_writes_attested: object,
    receipt_dir: object,
    project: object,
    folder: object,
    run_script: Callable[[dict[str, str]], Any],
) -> dict[str, Any]:
    """Apply every gate, then call the injected script runner. It holds no connection."""
    if not isinstance(action, str) or action not in _SCOPES:
        return _base(action, "blocked_invalid_action")
    if approved is not True:
        return _base(action, "blocked_until_approval")
    if approval_scope != _SCOPES[action]:
        return _base(action, "blocked_until_exact_approval_scope")
    for value in (project, folder):
        if not isinstance(value, str) or _PROJECT_RE.fullmatch(value) is None:
            return _base(action, "blocked_invalid_project_context")
    if action == "preflight":
        return _preflight(run_script, project, folder)
    if implementation_review_status != "approved":
        return _base(action, "blocked_until_implementation_review")
    if no_active_odi_session_writes_attested is not True:
        return _base(action, "blocked_until_session_attestation")
    if not isinstance(receipt_dir, (str, Path)):
        return _base(action, "blocked_receipt_directory")
    directory = Path(receipt_dir)
    if not directory.is_absolute() or not directory.is_dir():
        return _base(action, "blocked_receipt_directory")
    if action == "repair":
        return _repair(run_script, directory, project, folder)
    return _restore(run_script, directory, project, folder)
