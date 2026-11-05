"""Fixed DIM_PRODUCT audit-role and stable-expression preflight, repair and restore.

Role repair changes only the ODI SCD role of four audit columns. Stable repair also
replaces those four mapping expressions with a null-safe preservation profile. Neither
path touches a database row, any other column, an execute hint, or a scenario. A repair
makes any existing scenario stale until a separately reviewed new version exists.
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
    MAPPING_CURRENT_FLAG_EXPRESSION,
    MAPPING_EFFECTIVE_TO_EXPRESSION,
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
STABLE_PREFLIGHT_SCOPE = "odi87_dim_product_stable_audit_preflight"
STABLE_REPAIR_SCOPE = "odi87_dim_product_stable_audit_repair"
STABLE_RESTORE_SCOPE = "odi87_dim_product_stable_audit_restore"
RECEIPT_NAME = "odi87_dim_product_audit_role_receipt.json"
OUTCOME_NAME = "odi87_dim_product_audit_role_outcome.json"
RESTORE_NAME = "odi87_dim_product_audit_role_restore.json"

AUDIT_COLUMNS = ("INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID")
# The fixed builder writes these literals. Tests compare them with the Groovy source.
MAPPING_SURROGATE_EXPRESSION = "DWH.SEQ_DIM_PRODUCT.NEXTVAL"
MAPPING_EFFECTIVE_FROM_EXPRESSION = "CURRENT_DATE"
VOLATILE_PROFILE_ID = "odi87_dim_product_audit_volatile_v1"
STABLE_PROFILE_ID = "odi87_dim_product_audit_stable_v1"
_PROFILE_IDS = frozenset({VOLATILE_PROFILE_ID, STABLE_PROFILE_ID})
_ALIAS_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
# Known pre-stock mapping literals. They are a code gap, not a silent accept and not a rewrite.
LEGACY_CURRENT_FLAG_EXPRESSIONS = frozenset({"Y", "'Y'", "N", "'N'"})
LEGACY_EFFECTIVE_TO_EXPRESSIONS = frozenset(
    {
        "DATE '9999-12-31'",
        "TO_DATE('99991231','YYYYMMDD')",
        "to_date ('12-31-9999', 'mm-dd-yyyy')",
    }
)
NULL_ROLE = "NULL"
OVERWRITE_ROLE = "OVERWRITE_ON_CHANGE"
_ROLE_VALUES = frozenset({NULL_ROLE, OVERWRITE_ROLE})
_ROLE_ACTIONS = frozenset({"preflight", "repair", "restore"})
_STABLE_ACTIONS = frozenset({"stable_preflight", "stable_repair", "stable_restore"})
_SCOPES = {
    "preflight": PREFLIGHT_SCOPE,
    "repair": REPAIR_SCOPE,
    "restore": RESTORE_SCOPE,
    "stable_preflight": STABLE_PREFLIGHT_SCOPE,
    "stable_repair": STABLE_REPAIR_SCOPE,
    "stable_restore": STABLE_RESTORE_SCOPE,
}
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
        "fixed_expression_drift",
        "legacy_expression_gap",
        "source_alias_invalid",
        "audit_execute_hint_target",
        "execute_hint_drift",
        "expression_profile_drift",
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
        "changed_expressions",
        "expression_profile",
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


def _column_contract() -> dict[str, Any]:
    """Column names and types from the shared planner. No expression text."""
    plan = build_dim_product_scd2_plan(
        [
            {"name": name, "data_type": data_type, "nullable": nullable}
            for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
        ]
    )
    tracked = list(plan["tracked_attributes"])
    by_name = {column["name"]: column for column in plan["target_columns"]}
    return {
        "model_code": TARGET_MODEL_BY_OWNER[TARGET["owner"]],
        "mapping": MAPPING_NAME,
        "source_owner": SOURCE["owner"],
        "source_table": SOURCE["table"],
        "target_owner": TARGET["owner"],
        "target_table": TARGET["table"],
        "target_columns": [column["name"] for column in plan["target_columns"]],
        "source_columns": [*tracked, *plan["source_columns_excluded_from_change_detection"]],
        "tracked": tracked,
        "tracked_types": {name: by_name[name] for name in tracked},
        "surrogate_key": plan["surrogate_key"]["column"],
        "natural_key": plan["natural_key"][0],
        "start_date": "EFFECTIVE_FROM",
        "end_date": "EFFECTIVE_TO",
        "current_flag": "CURRENT_FLAG",
    }


def volatile_audit_expressions() -> dict[str, str]:
    """The four creation expressions the fixed mapping builder writes today."""
    return {
        "INSERT_DATE": MAPPING_INSERT_DATE_EXPRESSION,
        "UPDATE_DATE": MAPPING_UPDATE_DATE_EXPRESSION,
        "SOURCE_SYSTEM": MAPPING_SOURCE_SYSTEM_EXPRESSION,
        "ETL_BATCH_ID": MAPPING_ETL_BATCH_ID_EXPRESSION,
    }


def _canonical(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def profile_record(profile_id: str) -> dict[str, str]:
    """Return a known profile id and its hash. The hash preimage is not returned."""
    if profile_id == VOLATILE_PROFILE_ID:
        body: dict[str, Any] = {"id": profile_id, "expressions": volatile_audit_expressions()}
    elif profile_id == STABLE_PROFILE_ID:
        contract = _column_contract()
        body = {
            "id": profile_id,
            "alias": "validated_source_component_name",
            "ambiguity": "scalar_subquery_no_rownum",
            "comparison": "DECODE_NULL_SAFE_EQ_0",
            "current_flag_literal": "1",
            "else_expressions": volatile_audit_expressions(),
            "execute_hint": "preserve_existing_reject_target",
            "natural_key": contract["natural_key"],
            "target": f"{contract['target_owner']}.{contract['target_table']}",
            "tracked": list(contract["tracked"]),
        }
    else:
        raise ValueError("profile")
    return {"id": profile_id, "hash": _canonical(body)}


def fixed_expressions(alias: str) -> dict[str, str]:
    """The 22 expressions this repair must match and must not rewrite."""
    if not isinstance(alias, str) or _ALIAS_RE.fullmatch(alias) is None:
        raise ValueError("alias")
    contract = _column_contract()
    bound = {
        column: f"{alias}.{column}"
        for column in (contract["natural_key"], *contract["tracked"])
    }
    return {
        **bound,
        contract["surrogate_key"]: MAPPING_SURROGATE_EXPRESSION,
        "EFFECTIVE_FROM": MAPPING_EFFECTIVE_FROM_EXPRESSION,
        "EFFECTIVE_TO": MAPPING_EFFECTIVE_TO_EXPRESSION,
        "CURRENT_FLAG": MAPPING_CURRENT_FLAG_EXPRESSION,
    }


def fixed_expression_issue(name: str, text: object, alias: str) -> str | None:
    """Return the block code for one frozen expression, or None when it matches."""
    expected = fixed_expressions(alias)[name]
    if text == expected:
        return None
    if name == "CURRENT_FLAG" and text in LEGACY_CURRENT_FLAG_EXPRESSIONS:
        return "legacy_expression_gap"
    if name == "EFFECTIVE_TO" and text in LEGACY_EFFECTIVE_TO_EXPRESSIONS:
        return "legacy_expression_gap"
    return "fixed_expression_drift"


def stable_audit_expression(column: str, alias: str) -> str:
    """Preserve one current audit value when every tracked attribute still matches.

    The source alias is the validated component name. A second current row makes the
    scalar subquery fail. No ROWNUM hides that. The else branch is the frozen creation
    expression, so a new or changed version still receives a new audit value.
    """
    if column not in AUDIT_COLUMNS:
        raise ValueError("column")
    if not isinstance(alias, str) or _ALIAS_RE.fullmatch(alias) is None:
        raise ValueError("alias")
    contract = _column_contract()
    natural_key = str(contract["natural_key"])
    parts = [
        f"C.{natural_key} = {alias}.{natural_key}",
        "C.CURRENT_FLAG = '1'",
    ]
    parts.extend(
        f"DECODE(C.{name}, {alias}.{name}, 0, 1) = 0" for name in contract["tracked"]
    )
    predicate = " AND ".join(parts)
    target = f"{contract['target_owner']}.{contract['target_table']}"
    fallback = volatile_audit_expressions()[column]
    exists = f"SELECT 1 FROM {target} C WHERE {predicate}"
    preserved = f"SELECT C.{column} FROM {target} C WHERE {predicate}"
    return f"CASE WHEN EXISTS ({exists}) THEN ({preserved}) ELSE {fallback} END"


def expected_contract() -> dict[str, Any]:
    """The frozen column and expression contract, taken from the shared fixed sources."""
    contract = _column_contract()
    alias = contract["source_table"]
    return {
        **contract,
        "audit_expressions": volatile_audit_expressions(),
        "direct_columns": [contract["natural_key"], *contract["tracked"]],
        "literal_expressions": {
            contract["surrogate_key"]: MAPPING_SURROGATE_EXPRESSION,
            "EFFECTIVE_FROM": MAPPING_EFFECTIVE_FROM_EXPRESSION,
            "EFFECTIVE_TO": MAPPING_EFFECTIVE_TO_EXPRESSION,
            "CURRENT_FLAG": MAPPING_CURRENT_FLAG_EXPRESSION,
        },
        "fixed_expressions": fixed_expressions(alias),
        "stable_expressions": {column: stable_audit_expression(column, alias) for column in AUDIT_COLUMNS},
        "volatile_profile": profile_record(VOLATILE_PROFILE_ID),
        "stable_profile": profile_record(STABLE_PROFILE_ID),
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


def _ordered_audit_list(values: object) -> bool:
    if not isinstance(values, list) or any(type(item) is not str or item not in AUDIT_COLUMNS for item in values):
        return False
    if len(values) != len(set(values)):
        return False
    return values == [column for column in AUDIT_COLUMNS if column in set(values)]


def _profile_ok(payload: Mapping[str, Any]) -> bool:
    action, status, profile = payload["action"], payload["status"], payload["expression_profile"]
    if action in _ROLE_ACTIONS:
        return profile is None
    if action not in _STABLE_ACTIONS:
        return False
    if status in {"blocked", "aborted", "outcome_unknown"}:
        return profile is None or profile in _PROFILE_IDS
    if profile not in _PROFILE_IDS:
        return False
    repaired = status == "already_repaired" or (
        status in {"repaired", "committed_report_incomplete"} and action == "stable_repair"
    )
    return profile == STABLE_PROFILE_ID if repaired else True


def _images_consistent(payload: Mapping[str, Any]) -> bool:
    status, action = payload["status"], payload["action"]
    before, after = payload["before_image"], payload["after_image"]
    changed, expressions = payload["changed_columns"], payload["changed_expressions"]
    if not _ordered_audit_list(changed) or not _ordered_audit_list(expressions) or not _profile_ok(payload):
        return False
    if action in _ROLE_ACTIONS and expressions:
        return False
    if action in _STABLE_ACTIONS and expressions not in ([], list(AUDIT_COLUMNS)):
        return False
    if before is not None and not _image_valid(before):
        return False
    if after is not None and not _image_valid(after):
        return False
    if status == "ready_for_repair":
        if before is None or after is not None or changed or expressions:
            return False
        if action in _STABLE_ACTIONS:
            roles_open = NULL_ROLE in before.values()
            return roles_open or payload["expression_profile"] == VOLATILE_PROFILE_ID
        return NULL_ROLE in before.values()
    if status == "already_repaired":
        roles_done = before is not None and set(before.values()) == {OVERWRITE_ROLE}
        return roles_done and after is None and not changed and not expressions
    if status == "already_restored":
        return before is not None and after is None and not changed and not expressions
    if status in {"repaired", "restored", "committed_report_incomplete"}:
        if before is None or after is None or not (changed or expressions):
            return False
        repairing = action in {"repair", "stable_repair"}
        from_role, to_role = (NULL_ROLE, OVERWRITE_ROLE) if repairing else (OVERWRITE_ROLE, NULL_ROLE)
        roles_match = all(before[column] == from_role and after[column] == to_role for column in changed)
        unchanged = all(before[column] == after[column] for column in AUDIT_COLUMNS if column not in changed)
        return roles_match and unchanged
    return True


def _status_allowed_for_action(status: str, action: str) -> bool:
    allowed = {
        "ready_for_repair": {"preflight", "stable_preflight"},
        "already_repaired": {"preflight", "repair", "stable_preflight", "stable_repair"},
        "already_restored": {"restore", "stable_restore"},
        "repaired": {"repair", "stable_repair"},
        "restored": {"restore", "stable_restore"},
        "committed_report_incomplete": {"repair", "restore", "stable_repair", "stable_restore"},
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
        "changed_expressions": list(payload["changed_expressions"]),
        "expression_profile": payload["expression_profile"],
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


_STABLE_RECEIPT_FIELDS = (
    *_RECEIPT_FIELDS,
    "expression_profile_before",
    "expression_profile_target",
    "changed_expressions",
)
STABLE_RECEIPT_KEYS = frozenset({*_STABLE_RECEIPT_FIELDS, "digest"})


def _new_stable_receipt(before: Mapping[str, str], profile_id: str) -> dict[str, Any]:
    """Record roles and profile ids. The profile hash stands in for the SQL text."""
    if profile_id not in _PROFILE_IDS:
        raise ValueError("profile")
    changed = [column for column in AUDIT_COLUMNS if before[column] == NULL_ROLE]
    changed_expressions = [] if profile_id == STABLE_PROFILE_ID else list(AUDIT_COLUMNS)
    if not changed and not changed_expressions:
        raise ValueError("nothing_to_record")
    return {
        "schema_version": 2,
        "tool": TOOL,
        "scope": STABLE_REPAIR_SCOPE,
        "model": TARGET_MODEL_BY_OWNER[TARGET["owner"]],
        "mapping": MAPPING_NAME,
        "source": SOURCE["table"],
        "target": TARGET["table"],
        "before_image": dict(before),
        "target_image": {column: OVERWRITE_ROLE for column in AUDIT_COLUMNS},
        "changed_columns": changed,
        "expression_profile_before": profile_record(profile_id),
        "expression_profile_target": profile_record(STABLE_PROFILE_ID),
        "changed_expressions": changed_expressions,
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


def _read_stable_receipt(path: Path) -> dict[str, Any] | None:
    """Accept only a v2 receipt whose profile ids and hashes match this code."""
    try:
        receipt = _parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    if not isinstance(receipt, dict) or set(receipt) != STABLE_RECEIPT_KEYS or receipt.get("digest") != _digest(receipt):
        return None
    before = receipt.get("before_image")
    profile = receipt.get("expression_profile_before")
    if not _image_valid(before) or not isinstance(profile, dict) or set(profile) != {"id", "hash"}:
        return None
    try:
        expected = _new_stable_receipt(before, str(profile.get("id")))
    except (TypeError, ValueError):
        return None
    if any(receipt[key] != expected[key] for key in _STABLE_RECEIPT_FIELDS):
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


def _preflight(
    run_script: Callable[[dict[str, str]], Any],
    project: str,
    folder: str,
    action: str = "preflight",
) -> dict[str, Any]:
    output = call_sanitized(run_script, _script_parameters(action, _SCOPES[action], project, folder))
    if isinstance(output, str) and output.startswith("Hata: ODI Groovy action blocked"):
        return {**_base(action, "blocked_not_launched")}
    report = normalize_audit_role_report(_parse(output), action)
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


def _profile_parameters(profile_id: str, changed_columns: list[str], changed_expressions: list[str]) -> dict[str, str]:
    """Pass profile ids and column names. Expression text stays in the script."""
    if profile_id not in _PROFILE_IDS:
        raise ValueError("profile")
    return {
        "odi.audit.expression_profile_before": profile_id,
        "odi.audit.changed_expressions": ",".join(changed_expressions),
        "odi.audit.changed_columns": ",".join(changed_columns),
    }


def _stable_repair(run_script: Callable[[dict[str, str]], Any], directory: Path, project: str, folder: str) -> dict[str, Any]:
    receipt_path = directory / RECEIPT_NAME
    if receipt_path.exists():
        return _base("stable_repair", "blocked_receipt_exists")
    fresh = _preflight(run_script, project, folder, "stable_preflight")
    if fresh["status"] != "ready_for_repair":
        return {**fresh, "action": "stable_repair"}
    before = fresh["before_image"]
    profile_id = fresh["expression_profile"]
    try:
        receipt = _new_stable_receipt(before, profile_id)
    except (TypeError, ValueError):
        return {**_base("stable_repair", "blocked_receipt_invalid"), "repository_connection_attempted": True}
    if not _write_absent(receipt_path, receipt):
        return {**_base("stable_repair", "blocked_receipt_unwritable"), "repository_connection_attempted": True}
    output = call_sanitized(
        run_script,
        _script_parameters(
            "stable_repair",
            STABLE_REPAIR_SCOPE,
            project,
            folder,
            **{
                "odi.audit.expected_before": encode_image(before),
                **_profile_parameters(profile_id, receipt["changed_columns"], receipt["changed_expressions"]),
            },
        ),
    )
    report = {**_writer_report("stable_repair", output), "receipt_written": True}
    if report["status"] == "blocked_not_launched":
        return report
    if not _write_absent(directory / OUTCOME_NAME, _outcome_record(report, "stable_repair")):
        return _keep_commit_if_record_failed(report)
    return report


def _stable_restore(run_script: Callable[[dict[str, str]], Any], directory: Path, project: str, folder: str) -> dict[str, Any]:
    receipt_path = directory / RECEIPT_NAME
    if not receipt_path.exists():
        return _base("stable_restore", "blocked_receipt_missing")
    receipt = _read_stable_receipt(receipt_path)
    if receipt is None:
        return _base("stable_restore", "blocked_receipt_invalid")
    marker = directory / RESTORE_NAME
    if marker.exists():
        return _base("stable_restore", "blocked_receipt_stale")
    if _read_outcome(directory / OUTCOME_NAME) in _NO_EFFECT_OUTCOMES:
        return _base("stable_restore", "blocked_receipt_not_applied")
    profile_id = receipt["expression_profile_before"]["id"]
    output = call_sanitized(
        run_script,
        _script_parameters(
            "stable_restore",
            STABLE_RESTORE_SCOPE,
            project,
            folder,
            **{
                "odi.audit.expected_before": encode_image(receipt["before_image"]),
                **_profile_parameters(profile_id, receipt["changed_columns"], receipt["changed_expressions"]),
            },
        ),
    )
    report = _writer_report("stable_restore", output)
    if report["status"] in {"blocked", "aborted", "blocked_not_launched"}:
        return report
    if not _write_absent(marker, _outcome_record(report, "stable_restore")):
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
    if action in {"preflight", "stable_preflight"}:
        return _preflight(run_script, project, folder, action)
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
    if action == "stable_repair":
        return _stable_repair(run_script, directory, project, folder)
    if action == "stable_restore":
        return _stable_restore(run_script, directory, project, folder)
    return _restore(run_script, directory, project, folder)
