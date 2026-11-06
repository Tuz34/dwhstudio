"""ODI-87: the fixed audit-role image must cross the real run_groovy boundary.

The actual ``OdiOrchestrator.run_groovy`` parameter/approval/secret gates run
unmocked. Only external prerequisites are replaced: the Java launcher path (a
real interpreter), the local ODI properties helper and ``subprocess.run``.
Roles are synthetic enum values; no receipt, row, PK or configuration value is
read.
"""

from __future__ import annotations

import itertools
import json
import sys

import pytest

import dim_product_audit_role_repair as repair
import odi_orchestrator
from dim_product_audit_role_repair import (
    AUDIT_COLUMNS,
    NULL_ROLE,
    OVERWRITE_ROLE,
    SCRIPT_NAME,
    STABLE_PROFILE_ID,
    STABLE_REPAIR_SCOPE,
    VOLATILE_PROFILE_ID,
)
from odi_orchestrator import OdiOrchestrator, groovy_approval_context

KEY = "odi.audit.expected_before"
ROLES = (NULL_ROLE, OVERWRITE_ROLE)
ALL_IMAGES = [dict(zip(AUDIT_COLUMNS, combo)) for combo in itertools.product(ROLES, repeat=len(AUDIT_COLUMNS))]
GENERIC_PATTERN = r'^[a-zA-Z0-9._\-/\\ :,()#]+$'


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """Stub only the external process/config prerequisites of the real boundary."""
    props = tmp_path / "odi_runtime.properties"
    props.write_text("synthetic=1\n", encoding="utf-8")
    state = {"props_calls": 0, "processes": [], "stdout": ""}

    def ensure_props():
        state["props_calls"] += 1
        return str(props)

    def fake_run(command, **kwargs):
        state["processes"].append((command, kwargs))
        return _Completed(stdout=state["stdout"](command) if callable(state["stdout"]) else state["stdout"])

    monkeypatch.setattr(odi_orchestrator, "_JAVA_BIN", sys.executable)
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", ensure_props)
    monkeypatch.setattr(odi_orchestrator.subprocess, "run", fake_run)
    return state


def _params(image_text, **extra):
    values = {
        "odi.audit.action": "stable_repair",
        "odi.audit.approved": "true",
        "odi.audit.approval_scope": STABLE_REPAIR_SCOPE,
        "odi.project": "SYNTHETIC_PROJECT",
        "odi.folder": "SYNTHETIC_FOLDER",
        KEY: image_text,
    }
    values.update(extra)
    return values


def _run(script, params, *, approved=True):
    with groovy_approval_context(approved):
        return OdiOrchestrator().run_groovy(script, params)


def _blocked(result, error_type, state):
    assert result == f"Hata: ODI Groovy action blocked. error_type={error_type}"
    assert state["props_calls"] == 0 and state["processes"] == []


@pytest.mark.parametrize("image", ALL_IMAGES, ids=lambda image: "-".join(image[c][0] for c in AUDIT_COLUMNS))
def test_every_canonical_role_image_reaches_the_process_as_one_argument(boundary, image):
    text = repair.encode_image(image)
    _run(SCRIPT_NAME, _params(text))
    assert boundary["props_calls"] == 1 and len(boundary["processes"]) == 1
    command, kwargs = boundary["processes"][0]
    assert isinstance(command, list) and kwargs.get("shell", False) is False
    assert command.count(f"-D{KEY}={text}") == 1
    assert repair.decode_image(text) == image


UNSAFE = {
    "reordered": "UPDATE_DATE=NULL,INSERT_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "missing": "INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL",
    "extra": "INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL,PROD_NAME=NULL",
    "duplicate": "INSERT_DATE=NULL,INSERT_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "other_column": "PROD_NAME=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "unknown_role": "INSERT_DATE=NATURAL_KEY,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "lowercase_role": "INSERT_DATE=null,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "empty_role": "INSERT_DATE=,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "double_equals": "INSERT_DATE==NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "role_with_equals": "INSERT_DATE=NULL=X,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "leading_space": " INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "space_after_comma": "INSERT_DATE=NULL, UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "trailing_comma": "INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL,",
    "tab": "INSERT_DATE=NULL,\tUPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "newline": "INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL\n",
    "nul": "INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL\x00",
    "sql": "INSERT_DATE=NULL;DROP TABLE X,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "quoted": "INSERT_DATE='NULL',UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "path": "INSERT_DATE=..\\NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
    "unicode": "INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_İD=NULL",
    "empty": "",
    "expression": "INSERT_DATE=CURRENT_TIMESTAMP,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL",
}


@pytest.mark.parametrize("name", sorted(UNSAFE))
def test_non_canonical_images_stop_before_config_and_process(boundary, name):
    result = _run(SCRIPT_NAME, _params(UNSAFE[name]))
    _blocked(result, "InvalidParameterValue", boundary)
    assert repair.decode_image(UNSAFE[name]) is None


@pytest.mark.parametrize("value", [None, 0, 1.5, True, ["INSERT_DATE=NULL"], dict.fromkeys(AUDIT_COLUMNS, NULL_ROLE),
                                   b"INSERT_DATE=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL"])
def test_non_string_images_stop_before_config_and_process(boundary, value):
    _blocked(_run(SCRIPT_NAME, _params(value)), "InvalidParameterValue", boundary)
    assert repair.decode_image(value) is None


def test_the_image_shape_is_not_accepted_for_any_other_registered_script(boundary):
    text = repair.encode_image(ALL_IMAGES[0])
    others = sorted(name for name, spec in odi_orchestrator.GROOVY_ACTION_REGISTRY.items()
                    if name != SCRIPT_NAME and spec.generic_property_transport)
    assert others
    for script in others:
        result = _run(script, {KEY: text})
        refused = ("InvalidParameterValue", "ScriptNotFound", "LegacyRuntimeDisabled")
        assert any(result.endswith(f"error_type={code}") for code in refused), script
    assert boundary["props_calls"] == 0 and boundary["processes"] == []


@pytest.mark.parametrize("key", ["odi.audit.expected_after", "odi.audit.expected", "ODI.AUDIT.EXPECTED_BEFORE",
                                 "odi.audit.expected_before.x", "odi.expressions.audit"])
def test_other_keys_on_the_same_script_keep_the_generic_rule(boundary, key):
    params = _params(repair.encode_image(ALL_IMAGES[-1]))
    params[key] = params.pop(KEY)
    _blocked(_run(SCRIPT_NAME, params), "InvalidParameterValue", boundary)


def test_missing_approval_still_blocks_a_canonical_image(boundary):
    _blocked(_run(SCRIPT_NAME, _params(repair.encode_image(ALL_IMAGES[0])), approved=False),
             "ApprovalRequired", boundary)


@pytest.mark.parametrize("secret_key", ["odi.audit.password", "odi.token", "odi.jdbc.url", "odi.credential"])
def test_secret_keys_still_block_alongside_a_canonical_image(boundary, secret_key):
    params = _params(repair.encode_image(ALL_IMAGES[0]), **{secret_key: "SYNTHETIC"})
    _blocked(_run(SCRIPT_NAME, params), "SecretTransportRejected", boundary)


def test_generic_value_rule_is_unchanged_and_still_rejects_equals():
    assert odi_orchestrator._PARAM_VAL_RE.pattern == GENERIC_PATTERN
    assert odi_orchestrator._PARAM_VAL_RE.match("A=B") is None


def _report(action, **overrides):
    payload = {
        "tool": repair.TOOL, "mode": repair.RAW_MODE, "action": action, "status": "ready_for_repair",
        "code": None, "error_type": None, "side_effects": False, "repository_connection_attempted": True,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
        "repository_outcome": "no_write", "write_attempted": False, "commit_attempted": False,
        "commit_acknowledged": False, "rollback_attempted": True, "rollback_succeeded": True,
        "manual_recovery_review_required": False, "scenario_regeneration_required": False,
        "active_session_count": 0, "before_image": dict.fromkeys(AUDIT_COLUMNS, NULL_ROLE),
        "after_image": None, "changed_columns": [], "changed_expressions": [], "expression_profile": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_stable_repair_wiring_reaches_the_process_through_the_real_boundary(boundary, tmp_path):
    def stdout(command):
        action = next(arg.split("=", 1)[1] for arg in command if arg.startswith("-Dodi.audit.action="))
        if action == "stable_preflight":
            return _report("stable_preflight", expression_profile=VOLATILE_PROFILE_ID)
        return _report(
            "stable_repair", status="repaired", side_effects=True, repository_outcome="committed",
            write_attempted=True, commit_attempted=True, commit_acknowledged=True, rollback_attempted=False,
            rollback_succeeded=False, scenario_regeneration_required=True,
            after_image=dict.fromkeys(AUDIT_COLUMNS, OVERWRITE_ROLE), changed_columns=list(AUDIT_COLUMNS),
            changed_expressions=list(AUDIT_COLUMNS), expression_profile=STABLE_PROFILE_ID,
        )

    boundary["stdout"] = stdout
    result = OdiOrchestrator().dim_product_audit_role_action(
        action="stable_repair", approved=True, approval_scope=STABLE_REPAIR_SCOPE,
        implementation_review_status="approved", no_active_odi_session_writes_attested=True,
        receipt_dir=tmp_path,
    )
    assert result["status"] == "repaired", result
    actions = [next(a for a in cmd if a.startswith("-Dodi.audit.action=")) for cmd, _ in boundary["processes"]]
    assert actions == ["-Dodi.audit.action=stable_preflight", "-Dodi.audit.action=stable_repair"]
    repair_command = boundary["processes"][1][0]
    expected = repair.encode_image(dict.fromkeys(AUDIT_COLUMNS, NULL_ROLE))
    assert repair_command.count(f"-D{KEY}={expected}") == 1
    assert all(kwargs.get("shell", False) is False for _cmd, kwargs in boundary["processes"])
