"""Offline tests. The Groovy cases run the real script on an isolated fake SDK."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import dim_product_audit_role_repair as repair
from dim_product_audit_role_repair import (
    AUDIT_COLUMNS,
    PREFLIGHT_SCOPE,
    REPAIR_SCOPE,
    RESTORE_SCOPE,
    STABLE_PREFLIGHT_SCOPE,
    STABLE_PROFILE_ID,
    STABLE_REPAIR_SCOPE,
    STABLE_RESTORE_SCOPE,
    VOLATILE_PROFILE_ID,
    SCRIPT_NAME,
    expected_contract,
    fixed_expression_issue,
    normalize_audit_role_report,
    run_audit_role_action,
    stable_audit_expression,
)
from tests import dim_product_audit_role_fake_sdk as fake

ALL_NULL = {column: "NULL" for column in AUDIT_COLUMNS}
ALL_OVERWRITE = {column: "OVERWRITE_ON_CHANGE" for column in AUDIT_COLUMNS}
SCOPES = {
    "preflight": PREFLIGHT_SCOPE,
    "repair": REPAIR_SCOPE,
    "restore": RESTORE_SCOPE,
    "stable_preflight": STABLE_PREFLIGHT_SCOPE,
    "stable_repair": STABLE_REPAIR_SCOPE,
    "stable_restore": STABLE_RESTORE_SCOPE,
}
LEAK_PATTERN = re.compile(r"RAISE_TOKEN|EXPRESSION_TOKEN|jdbc|CURRENT_TIMESTAMP|SESS_NO|odiRef|internal_id", re.I)


def params(action="preflight", *, before=None, changed=None, profile=None, expressions=None, **overrides):
    values = {
        "odi.audit.action": action,
        "odi.audit.approved": "true",
        "odi.audit.approval_scope": SCOPES[action],
        "odi.project": "PRJ",
        "odi.folder": "FLD",
    }
    if before is not None:
        values["odi.audit.expected_before"] = repair.encode_image(before)
    if changed is not None:
        values["odi.audit.changed_columns"] = ",".join(changed)
    if profile is not None:
        values["odi.audit.expression_profile_before"] = profile
    if expressions is not None:
        values["odi.audit.changed_expressions"] = ",".join(expressions)
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def overwrite_roles():
    return {column: "OVERWRITE_ON_CHANGE" for column in AUDIT_COLUMNS}


PREFLIGHT = "preflight"
REPAIR_ALL = dict(action="repair", before=ALL_NULL)
RESTORE_ALL = dict(action="restore", before=ALL_NULL, changed=list(AUDIT_COLUMNS))

SCENARIOS = {
    # Successful and no-op paths.
    "preflight_ready": {"params": params(), "spec": {}},
    "preflight_already": {"params": params(), "spec": {"roles": overwrite_roles()}},
    "preflight_partial": {"params": params(), "spec": {"roles": {"INSERT_DATE": "OVERWRITE_ON_CHANGE"}}},
    "repair_all": {"params": params(**REPAIR_ALL), "spec": {}},
    "repair_partial": {
        "params": params("repair", before={**ALL_NULL, "INSERT_DATE": "OVERWRITE_ON_CHANGE"}),
        "spec": {"roles": {"INSERT_DATE": "OVERWRITE_ON_CHANGE"}},
    },
    "repair_already": {"params": params("repair", before=ALL_OVERWRITE), "spec": {"roles": overwrite_roles()}},
    "restore_all": {"params": params(**RESTORE_ALL), "spec": {"roles": overwrite_roles()}},
    "restore_only_changed": {
        "params": params("restore", before={**ALL_NULL, "INSERT_DATE": "OVERWRITE_ON_CHANGE"}, changed=[c for c in AUDIT_COLUMNS if c != "INSERT_DATE"]),
        "spec": {"roles": overwrite_roles()},
    },
    "restore_already": {"params": params(**RESTORE_ALL), "spec": {}},
    "standalone_clean": {"params": params(**REPAIR_ALL), "spec": {"standalone": True}},
    # Fixed object and drift blocks. Each must stop without a commit.
    "project_missing": {"params": params(**REPAIR_ALL), "spec": {"project_count": 0}},
    "project_ambiguous": {"params": params(**REPAIR_ALL), "spec": {"project_count": 2}},
    "project_case_differs": {"params": params(**REPAIR_ALL), "spec": {"project_code": "prj"}},
    "folder_missing": {"params": params(**REPAIR_ALL), "spec": {"folder_count": 0}},
    "folder_ambiguous": {"params": params(**REPAIR_ALL), "spec": {"folder_count": 2}},
    "model_missing": {"params": params(**REPAIR_ALL), "spec": {"model_count": 0}},
    "model_ambiguous": {"params": params(**REPAIR_ALL), "spec": {"model_count": 2}},
    "model_not_oracle": {"params": params(**REPAIR_ALL), "spec": {"model_technology": "PostgreSQL"}},
    "target_missing": {"params": params(**REPAIR_ALL), "spec": {"target_count": 0}},
    "target_ambiguous": {"params": params(**REPAIR_ALL), "spec": {"target_count": 2}},
    "source_missing": {"params": params(**REPAIR_ALL), "spec": {"source_count": 0}},
    "source_ambiguous": {"params": params(**REPAIR_ALL), "spec": {"source_count": 2}},
    "mapping_missing": {"params": params(**REPAIR_ALL), "spec": {"mapping_missing": True}},
    "mapping_target_redirect": {"params": params(**REPAIR_ALL), "spec": {"target_redirect": True}},
    "mapping_source_redirect": {"params": params(**REPAIR_ALL), "spec": {"source_redirect": True}},
    "mapping_target_other_model": {"params": params(**REPAIR_ALL), "spec": {"target_other_model": True}},
    "mapping_extra_target": {"params": params(**REPAIR_ALL), "spec": {"extra_target_component": True}},
    "mapping_not_scd": {"params": params(**REPAIR_ALL), "spec": {"integration_type": "CONTROL_APPEND"}},
    "column_missing": {"params": params(**REPAIR_ALL), "spec": {"remove_columns": ["UPDATE_DATE"]}},
    "column_tracked_missing": {"params": params(**REPAIR_ALL), "spec": {"remove_columns": ["PROD_NAME"]}},
    "column_extra": {"params": params(**REPAIR_ALL), "spec": {"extra_columns": ["UNEXPECTED_COLUMN"]}},
    "column_duplicate": {"params": params(**REPAIR_ALL), "spec": {"duplicate_column": "UPDATE_DATE"}},
    "column_swapped": {"params": params(**REPAIR_ALL), "spec": {"remove_columns": ["PROD_DESC"], "extra_columns": ["UNEXPECTED_COLUMN"]}},
    "source_column_missing": {"params": params(**REPAIR_ALL), "spec": {"remove_source_columns": ["PROD_NAME"]}},
    "attribute_missing": {"params": params(**REPAIR_ALL), "spec": {"remove_attributes": ["PROD_NAME"]}},
    "role_sk_wrong": {"params": params(**REPAIR_ALL), "spec": {"roles": {"PRODUCT_SK": "ADD_ROW_ON_CHANGE"}}},
    "role_nk_wrong": {"params": params(**REPAIR_ALL), "spec": {"roles": {"PROD_ID": "ADD_ROW_ON_CHANGE"}}},
    "role_start_wrong": {"params": params(**REPAIR_ALL), "spec": {"roles": {"EFFECTIVE_FROM": "END_TIMESTAMP"}}},
    "role_end_wrong": {"params": params(**REPAIR_ALL), "spec": {"roles": {"EFFECTIVE_TO": None}}},
    "role_current_wrong": {"params": params(**REPAIR_ALL), "spec": {"roles": {"CURRENT_FLAG": "OVERWRITE_ON_CHANGE"}}},
    "role_tracked_null": {"params": params(**REPAIR_ALL), "spec": {"roles": {"PROD_NAME": None}}},
    "role_tracked_overwrite": {"params": params(**REPAIR_ALL), "spec": {"roles": {"PROD_DESC": "OVERWRITE_ON_CHANGE"}}},
    "audit_role_unknown": {"params": params(**REPAIR_ALL), "spec": {"roles": {"INSERT_DATE": "ADD_ROW_ON_CHANGE"}}},
    "audit_role_natural_key": {"params": params(**REPAIR_ALL), "spec": {"roles": {"ETL_BATCH_ID": "NATURAL_KEY"}}},
    "before_image_drift": {"params": params(**REPAIR_ALL), "spec": {"roles": {"SOURCE_SYSTEM": "OVERWRITE_ON_CHANGE"}}},
    "expression_drift": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"SOURCE_SYSTEM": "'" + fake.EXPRESSION_TOKEN + "'"}}},
    "expression_missing": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"ETL_BATCH_ID": None}}},
    "other_expression_missing": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"PROD_NAME": None}}},
    "active_running": {"params": params(**REPAIR_ALL), "spec": {"active_sessions": {"RUNNING": 1}}},
    "active_waiting": {"params": params(**REPAIR_ALL), "spec": {"active_sessions": {"WAITING": 1}}},
    "active_queued": {"params": params(**REPAIR_ALL), "spec": {"active_sessions": {"QUEUED": 2}}},
    "restore_current_drift": {"params": params(**RESTORE_ALL), "spec": {"roles": {"INSERT_DATE": "OVERWRITE_ON_CHANGE"}}},
    "restore_unknown_role": {"params": params(**RESTORE_ALL), "spec": {"roles": {**overwrite_roles(), "ETL_BATCH_ID": "ADD_ROW_ON_CHANGE"}}},
    "restore_unchanged_column_drift": {
        "params": params("restore", before={**ALL_NULL, "INSERT_DATE": "OVERWRITE_ON_CHANGE"}, changed=[c for c in AUDIT_COLUMNS if c != "INSERT_DATE"]),
        "spec": {"roles": {column: "OVERWRITE_ON_CHANGE" for column in AUDIT_COLUMNS if column != "INSERT_DATE"}},
    },
    # Faults inside the write, after validation.
    "setter_raises_midway": {"params": params(**REPAIR_ALL), "spec": {"setter_raises_on": "SOURCE_SYSTEM"}},
    "setter_ignored": {"params": params(**REPAIR_ALL), "spec": {"setter_ignored_on": "UPDATE_DATE"}},
    "flush_raises": {"params": params(**REPAIR_ALL), "spec": {"flush_raises": True}},
    "session_finder_raises": {"params": params(**REPAIR_ALL), "spec": {"session_finder_raises": True}},
    "begin_raises": {"params": params(**REPAIR_ALL), "spec": {"begin_raises": True}},
    # Commit and rollback uncertainty.
    "commit_raises_not_applied": {"params": params(**REPAIR_ALL), "spec": {"commit_raises": True}},
    "commit_applied_then_raises": {"params": params(**REPAIR_ALL), "spec": {"commit_applied_then_raises": True}},
    "commit_applied_rollback_fails": {"params": params(**REPAIR_ALL), "spec": {"commit_applied_then_raises": True, "rollback_raises": True}},
    "commit_applied_then_error": {"params": params(**REPAIR_ALL), "spec": {"commit_applied_then_error": True}},
    "restore_commit_applied_then_raises": {"params": params(**RESTORE_ALL), "spec": {"roles": overwrite_roles(), "commit_applied_then_raises": True}},
    "block_rollback_fails": {"params": params(**REPAIR_ALL), "spec": {"mapping_missing": True, "rollback_raises": True}},
    "write_fault_rollback_fails": {"params": params(**REPAIR_ALL), "spec": {"flush_raises": True, "rollback_raises": True}},
    "preflight_rollback_fails": {"params": params(), "spec": {"rollback_raises": True}},
    "success_report_print_fails": {"params": params(**REPAIR_ALL), "spec": {"print_failures": 1}},
    "standalone_close_raises_after_commit": {"params": params(**REPAIR_ALL), "spec": {"standalone": True, "close_raises": True}},
    "standalone_close_raises_after_unknown": {"params": params(**REPAIR_ALL), "spec": {"standalone": True, "close_raises": True, "commit_applied_then_raises": True}},
    "standalone_close_raises_after_block": {"params": params(**REPAIR_ALL), "spec": {"standalone": True, "close_raises": True, "mapping_missing": True}},
    # Refused before any repository call.
    "script_not_approved": {"params": params(**{"odi.audit.approved": "false"}), "spec": {}},
    "script_approved_missing": {"params": params(**{"odi.audit.approved": None}), "spec": {}},
    "script_scope_wrong": {"params": params(**{"odi.audit.approval_scope": REPAIR_SCOPE}), "spec": {}},
    "script_repair_with_preflight_scope": {"params": params("repair", before=ALL_NULL, **{"odi.audit.approval_scope": PREFLIGHT_SCOPE}), "spec": {}},
    "script_action_unknown": {"params": {**params(), "odi.audit.action": "delete"}, "spec": {}},
    "script_project_missing": {"params": params(**{"odi.project": None}), "spec": {}},
    "script_project_injection": {"params": params(**{"odi.project": 'PRJ"; DROP'}), "spec": {}},
    "script_before_missing": {"params": params("repair"), "spec": {}},
    "script_before_unknown_role": {"params": params("repair", **{"odi.audit.expected_before": "INSERT_DATE=NATURAL_KEY,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL"}), "spec": {}},
    "script_before_other_column": {"params": params("repair", **{"odi.audit.expected_before": "PROD_NAME=NULL,UPDATE_DATE=NULL,SOURCE_SYSTEM=NULL,ETL_BATCH_ID=NULL"}), "spec": {}},
    "script_restore_changed_missing": {"params": params("restore", before=ALL_NULL), "spec": {}},
    "script_restore_changed_other_column": {"params": params("restore", before=ALL_NULL, changed=["PROD_NAME"]), "spec": {}},
    "script_restore_changed_not_null_before": {"params": params("restore", before=ALL_OVERWRITE, changed=["INSERT_DATE"]), "spec": {}},
    "script_stable_profile_sql": {
        "params": params(
            "stable_repair",
            before=ALL_NULL,
            changed=list(AUDIT_COLUMNS),
            expressions=list(AUDIT_COLUMNS),
            **{
                "odi.audit.expression_profile_before": "SELECT SECRET",
                "odi.audit.expression_sql": "CASE WHEN DECODE(SECRET) THEN SELECT SECRET END",
            },
        ),
        "spec": {},
    },
    "stable_preflight_volatile": {"params": params("stable_preflight"), "spec": {}},
    "stable_repair_all": {
        "params": params("stable_repair", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {},
    },
    "stable_repair_expressions_only": {
        "params": params("stable_repair", before=ALL_OVERWRITE, changed=[], expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"roles": overwrite_roles()},
    },
    "stable_repair_partial_roles": {
        "params": params(
            "stable_repair",
            before={**ALL_NULL, "INSERT_DATE": "OVERWRITE_ON_CHANGE"},
            changed=[column for column in AUDIT_COLUMNS if column != "INSERT_DATE"],
            expressions=list(AUDIT_COLUMNS),
            profile=VOLATILE_PROFILE_ID,
        ),
        "spec": {"roles": {"INSERT_DATE": "OVERWRITE_ON_CHANGE"}},
    },
    "stable_repair_already": {
        "params": params("stable_repair", before=ALL_OVERWRITE, changed=[], expressions=[], profile=STABLE_PROFILE_ID),
        "spec": {"roles": overwrite_roles(), "audit_profile": "stable"},
    },
    "stable_repair_rebind_alias": {
        "params": params("stable_repair", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"source_alias": "SRC_ALIAS", "rebind_alias": True},
    },
    "stable_restore_to_volatile": {
        "params": params("stable_restore", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"roles": overwrite_roles(), "audit_profile": "stable"},
    },
    "stable_restore_already": {
        "params": params("stable_restore", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {},
    },
    "legacy_flag": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"CURRENT_FLAG": "Y"}}},
    "legacy_end": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"EFFECTIVE_TO": "DATE '9999-12-31'"}}},
    "unknown_end": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"EFFECTIVE_TO": "DATE '1999-01-01'"}}},
    "wrong_tracked_expression": {"params": params(**REPAIR_ALL), "spec": {"expressions": {"PROD_NAME": "SRC_ALIAS.PROD_NAME"}}},
    "audit_hint_target": {"params": params(**REPAIR_ALL), "spec": {"execute_hints": {"INSERT_DATE": "TARGET"}}},
    "sk_hint_missing": {"params": params(**REPAIR_ALL), "spec": {"execute_hints": {"PRODUCT_SK": "NO_HINT"}}},
    "bad_source_alias": {"params": params(**REPAIR_ALL), "spec": {"source_alias": "bad-name"}},
    "stable_active_session": {
        "params": params("stable_repair", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"active_sessions": {"RUNNING": 1}},
    },
    "stable_commit_applied_then_raises": {
        "params": params("stable_repair", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"commit_applied_then_raises": True},
    },
    "stable_expression_setter_raises": {
        "params": params("stable_repair", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"expression_setter_raises_on": "SOURCE_SYSTEM"},
    },
    "stable_rollback_fails": {
        "params": params("stable_repair", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"expression_setter_raises_on": "INSERT_DATE", "rollback_raises": True},
    },
    "stable_foreign_expression": {
        "params": params("stable_restore", before=ALL_NULL, changed=list(AUDIT_COLUMNS), expressions=list(AUDIT_COLUMNS), profile=VOLATILE_PROFILE_ID),
        "spec": {"roles": overwrite_roles(), "audit_profile": "stable", "expressions": {"PROD_DESC": "OTHER.PROD_DESC"}},
    },
}

NO_WRITE_BLOCKS = [
    "project_missing", "project_ambiguous", "project_case_differs", "folder_missing", "folder_ambiguous",
    "model_missing", "model_ambiguous", "model_not_oracle", "target_missing", "target_ambiguous", "source_missing",
    "source_ambiguous", "mapping_missing", "mapping_target_redirect", "mapping_source_redirect",
    "mapping_target_other_model", "mapping_extra_target", "mapping_not_scd", "column_missing",
    "column_tracked_missing", "column_extra", "column_duplicate", "column_swapped", "source_column_missing", "attribute_missing",
    "role_sk_wrong", "role_nk_wrong", "role_start_wrong", "role_end_wrong", "role_current_wrong", "role_tracked_null",
    "role_tracked_overwrite", "audit_role_unknown", "audit_role_natural_key", "before_image_drift", "expression_drift",
    "expression_missing", "other_expression_missing", "active_running", "active_waiting", "active_queued",
    "restore_current_drift", "restore_unknown_role", "restore_unchanged_column_drift",
    "legacy_flag", "legacy_end", "unknown_end", "wrong_tracked_expression", "audit_hint_target",
    "sk_hint_missing", "bad_source_alias", "stable_active_session", "stable_foreign_expression",
]
REFUSED_BEFORE_CONNECTION = [name for name in SCENARIOS if name.startswith("script_")]


@pytest.fixture(scope="module")
def java():
    binary = fake.available()
    if binary is None:
        pytest.skip("Local Java/Groovy runtime not available; no runtime is downloaded")
    return binary


@pytest.fixture(scope="module")
def runtime(java, tmp_path_factory):
    workspace = tmp_path_factory.mktemp("audit_role_runtime")
    return workspace, fake.prepare_runtime(java, workspace)


@pytest.fixture(scope="module")
def runs(java, runtime):
    return fake.run_scenarios(java, runtime[0], runtime[1], SCENARIOS)


def payload_of(run):
    lines = [line for line in run["output"].splitlines() if line.strip()]
    assert len(lines) == 1, "The script must emit exactly one structured line"
    return json.loads(lines[0])


def action_of(name):
    return SCENARIOS[name]["params"].get("odi.audit.action")


def writes(run):
    return [event for event in run["trace"] if event.startswith("setScdType")]


def test_every_scenario_produces_one_normalizable_report(runs):
    for name, run in runs.items():
        action = action_of(name)
        if action not in SCOPES:
            action = "preflight"
        report = normalize_audit_role_report(payload_of(run), action)
        assert report["status"] != "blocked_invalid_output", name


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_no_raw_expression_exception_or_identifier_leaks(runs, name):
    assert LEAK_PATTERN.search(runs[name]["output"]) is None


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_only_the_four_audit_roles_can_ever_be_written(runs, name):
    run = runs[name]
    assert "forbidden_write" not in run["trace"]
    for event in writes(run):
        assert event.split(":")[1].split("=")[0] in AUDIT_COLUMNS


def test_preflight_ready_reads_only_and_reports_the_before_image(runs):
    run = runs["preflight_ready"]
    payload = payload_of(run)
    assert payload["status"] == "ready_for_repair"
    assert payload["repository_outcome"] == "no_write"
    assert payload["side_effects"] is False
    assert payload["before_image"] == ALL_NULL
    assert payload["changed_columns"] == []
    assert writes(run) == [] and "commit" not in run["trace"] and "flush" not in run["trace"]
    assert run["trace"][-1] == "rollback"


def test_preflight_recognizes_repaired_and_partial_states(runs):
    assert payload_of(runs["preflight_already"])["status"] == "already_repaired"
    partial = payload_of(runs["preflight_partial"])
    assert partial["status"] == "ready_for_repair"
    assert partial["before_image"]["INSERT_DATE"] == "OVERWRITE_ON_CHANGE"
    assert partial["before_image"]["UPDATE_DATE"] == "NULL"


def test_repair_writes_commits_and_records_exact_images(runs):
    run = runs["repair_all"]
    payload = payload_of(run)
    assert payload["status"] == "repaired" and payload["side_effects"] is True
    assert payload["before_image"] == ALL_NULL and payload["after_image"] == ALL_OVERWRITE
    assert payload["changed_columns"] == list(AUDIT_COLUMNS)
    assert payload["scenario_regeneration_required"] is True
    assert payload["manual_recovery_review_required"] is False
    assert run["committed"] is True
    trace = run["trace"]
    assert trace.index("setScdType:INSERT_DATE=OVERWRITE_ON_CHANGE") < trace.index("flush") < trace.index("commit")
    assert trace[-1] == "commit"
    for column in AUDIT_COLUMNS:
        assert run["persisted"][column] == "OVERWRITE_ON_CHANGE"
    assert run["persisted"]["PROD_NAME"] == "ADD_ROW_ON_CHANGE"
    assert run["persisted"]["PRODUCT_SK"] == "SURROGATE_KEY"


def test_partial_repair_writes_only_the_null_columns(runs):
    run = runs["repair_partial"]
    payload = payload_of(run)
    assert payload["status"] == "repaired"
    assert payload["changed_columns"] == [column for column in AUDIT_COLUMNS if column != "INSERT_DATE"]
    assert not any(event.startswith("setScdType:INSERT_DATE") for event in run["trace"])


def test_repair_on_repaired_state_is_a_noop_without_commit(runs):
    run = runs["repair_already"]
    assert payload_of(run)["status"] == "already_repaired"
    assert writes(run) == [] and run["committed"] is False


def test_restore_reverts_only_what_the_receipt_says_changed(runs):
    run = runs["restore_only_changed"]
    payload = payload_of(run)
    assert payload["status"] == "restored"
    assert payload["changed_columns"] == [column for column in AUDIT_COLUMNS if column != "INSERT_DATE"]
    assert run["persisted"]["INSERT_DATE"] == "OVERWRITE_ON_CHANGE"
    assert run["persisted"]["UPDATE_DATE"] is None
    assert payload["scenario_regeneration_required"] is True


def test_restore_all_and_already_restored(runs):
    run = runs["restore_all"]
    assert payload_of(run)["status"] == "restored"
    assert all(run["persisted"][column] is None for column in AUDIT_COLUMNS)
    already = runs["restore_already"]
    assert payload_of(already)["status"] == "already_restored"
    assert writes(already) == [] and already["committed"] is False


def test_standalone_connection_branch_runs_the_same_path(runs):
    run = runs["standalone_clean"]
    assert payload_of(run)["status"] == "repaired"
    assert run["trace"][-1] == "odi_close"


@pytest.mark.parametrize("name", NO_WRITE_BLOCKS)
def test_drift_blocks_with_no_write_and_no_commit(runs, name):
    run = runs[name]
    payload = payload_of(run)
    assert payload["status"] == "blocked", (name, payload)
    assert payload["repository_outcome"] == "rolled_back"
    assert payload["side_effects"] is False
    assert writes(run) == []
    assert "commit" not in run["trace"]
    assert run["committed"] is False


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("project_ambiguous", "project_contract"),
        ("mapping_target_redirect", "mapping_contract"),
        ("column_extra", "column_contract"),
        ("column_duplicate", "column_contract"),
        ("column_swapped", "column_contract"),
        ("role_tracked_null", "role_contract"),
        ("audit_role_unknown", "audit_role_unexpected"),
        ("before_image_drift", "before_image_drift"),
        ("expression_drift", "audit_expression_drift"),
        ("active_running", "active_odi_sessions"),
    ],
)
def test_blocks_carry_fixed_codes(runs, name, code):
    assert payload_of(runs[name])["code"] == code


def test_active_session_count_is_a_number_only(runs):
    assert payload_of(runs["active_queued"])["active_session_count"] == 2


@pytest.mark.parametrize("name", REFUSED_BEFORE_CONNECTION)
def test_bad_script_inputs_are_refused_before_any_repository_call(runs, name):
    run = runs[name]
    payload = payload_of(run)
    assert payload["status"] == "blocked"
    assert payload["repository_connection_attempted"] is False
    assert payload["repository_outcome"] == "no_transaction"
    assert run["trace"] == []


@pytest.mark.parametrize("name", ["setter_raises_midway", "setter_ignored", "flush_raises"])
def test_failures_inside_the_write_roll_back_every_column(runs, name):
    run = runs[name]
    payload = payload_of(run)
    assert payload["repository_outcome"] == "rolled_back"
    assert payload["side_effects"] is False
    assert payload["rollback_succeeded"] is True
    assert "commit" not in run["trace"]
    assert all(run["persisted"][column] is None for column in AUDIT_COLUMNS)


def test_partial_setter_failure_reports_the_class_only(runs):
    payload = payload_of(runs["setter_raises_midway"])
    assert payload["status"] == "aborted"
    assert payload["error_type"] == "RuntimeException"
    assert payload["write_attempted"] is True


def test_setter_with_no_effect_is_caught_by_the_recheck(runs):
    payload = payload_of(runs["setter_ignored"])
    assert payload["status"] == "blocked" and payload["code"] == "post_write_verification"


def test_failure_before_the_transaction_is_a_clean_abort(runs):
    payload = payload_of(runs["begin_raises"])
    assert payload["status"] == "aborted"
    assert payload["repository_outcome"] == "no_transaction"
    assert payload["rollback_attempted"] is False


@pytest.mark.parametrize(
    ("name", "applied", "rollback_succeeded"),
    [
        ("commit_raises_not_applied", False, True),
        ("commit_applied_then_raises", True, True),
        ("commit_applied_rollback_fails", True, False),
        ("commit_applied_then_error", True, True),
        ("restore_commit_applied_then_raises", True, True),
        ("standalone_close_raises_after_unknown", True, True),
    ],
)
def test_uncertain_commit_stays_unknown_whatever_the_rollback_did(runs, name, applied, rollback_succeeded):
    run = runs[name]
    payload = payload_of(run)
    assert run["committed"] is applied
    assert run["script_raised"] is None
    assert payload["status"] == "outcome_unknown"
    assert payload["repository_outcome"] == "commit_outcome_unknown"
    assert payload["side_effects"] is None
    assert payload["commit_attempted"] is True and payload["commit_acknowledged"] is False
    assert payload["rollback_succeeded"] is rollback_succeeded
    assert payload["manual_recovery_review_required"] is True
    assert normalize_audit_role_report(payload, action_of(name))["side_effects"] is None


def test_applied_and_unapplied_commit_failures_report_the_same_thing(runs):
    assert payload_of(runs["commit_raises_not_applied"]) == payload_of(runs["commit_applied_then_raises"])


@pytest.mark.parametrize("name", ["block_rollback_fails", "write_fault_rollback_fails", "preflight_rollback_fails"])
def test_failed_rollback_is_never_a_clean_result(runs, name):
    payload = payload_of(runs[name])
    assert payload["status"] == "outcome_unknown"
    assert payload["repository_outcome"] == "rollback_failed"
    assert payload["side_effects"] is None
    assert payload["commit_attempted"] is False
    assert payload["manual_recovery_review_required"] is True


def test_acknowledged_commit_with_a_lost_report_keeps_the_commit(runs):
    run = runs["success_report_print_fails"]
    payload = payload_of(run)
    assert run["committed"] is True and "rollback" not in run["trace"]
    assert payload["status"] == "committed_report_incomplete"
    assert payload["repository_outcome"] == "committed"
    assert payload["side_effects"] is True
    assert payload["manual_recovery_review_required"] is True
    assert payload["scenario_regeneration_required"] is True


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("standalone_close_raises_after_commit", "repaired"),
        ("standalone_close_raises_after_unknown", "outcome_unknown"),
        ("standalone_close_raises_after_block", "blocked"),
    ],
)
def test_instance_close_failure_cannot_replace_the_outcome(runs, name, status):
    run = runs[name]
    assert run["script_raised"] is None
    assert payload_of(run)["status"] == status
    assert run["trace"][-1] == "odi_close"


def test_python_contract_comes_from_the_shared_sources():
    from dim_product_scd2_stock_contract import (
        MAPPING_ETL_BATCH_ID_EXPRESSION,
        MAPPING_INSERT_DATE_EXPRESSION,
        MAPPING_SOURCE_SYSTEM_EXPRESSION,
        MAPPING_UPDATE_DATE_EXPRESSION,
    )
    from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

    plan = build_dim_product_scd2_plan(
        [{"name": n, "data_type": t, "nullable": null} for n, t, null in PRODUCTS_SOURCE_METADATA]
    )
    contract = expected_contract()
    assert contract["tracked"] == list(plan["tracked_attributes"])
    assert len(contract["tracked"]) == 17 and len(contract["target_columns"]) == 26
    assert contract["audit_expressions"] == {
        "INSERT_DATE": MAPPING_INSERT_DATE_EXPRESSION,
        "UPDATE_DATE": MAPPING_UPDATE_DATE_EXPRESSION,
        "SOURCE_SYSTEM": MAPPING_SOURCE_SYSTEM_EXPRESSION,
        "ETL_BATCH_ID": MAPPING_ETL_BATCH_ID_EXPRESSION,
    }


def test_builder_and_registrar_scripts_use_the_same_column_lists():
    root = Path(repair.__file__).resolve().parent / "odi_automation"
    registrar = (root / "dim_product_scd2_datastore_register.groovy").read_text(encoding="utf-8")
    builder = (root / "dim_product_scd2_mapping_create.groovy").read_text(encoding="utf-8")
    contract = expected_contract()
    assert re.findall(r'\[name: "([A-Z_]+)"', registrar) == contract["target_columns"]
    for column in contract["tracked"]:
        assert f'"{column}"' in builder
    for column, expression in contract["audit_expressions"].items():
        escaped = expression.replace('"', '\\"')
        assert f'{column}: "{escaped}"' in builder
    assert 'PRODUCT_SK: "DWH.SEQ_DIM_PRODUCT.NEXTVAL"' in builder
    assert 'EFFECTIVE_FROM: "CURRENT_DATE"' in builder
    assert "TO_DATE('24000101','YYYYMMDD')" in builder
    assert 'CURRENT_FLAG: "1"' in builder
    repair_script = (root / "dim_product_audit_role_repair.groovy").read_text(encoding="utf-8")
    assert re.search(r"TRACKED_COLUMNS = \[(.*?)\]", repair_script, re.S).group(1).split()
    tracked_text = re.search(r"final List<String> TRACKED_COLUMNS = \[(.*?)\]", repair_script, re.S).group(1)
    assert re.findall(r'"([A-Z0-9_]+)"', tracked_text) == contract["tracked"]


def test_script_uses_only_public_methods_of_the_installed_sdk(java, tmp_path):
    if not (fake.SDK_LIB / "odi-core.jar").is_file() or not fake.ECLIPSELINK_JAR.is_file():
        pytest.skip("Local ODI SDK not available; no runtime is downloaded")
    string = "java.lang.String"
    required = [
        {"type": "oracle.odi.domain.model.OdiColumn", "method": "setScdType", "params": ["oracle.odi.domain.model.OdiColumn$ScdType"]},
        {"type": "oracle.odi.domain.model.OdiColumn", "method": "getScdType", "params": []},
        {"type": "oracle.odi.domain.model.OdiDataStore", "method": "getColumns", "params": []},
        {"type": "oracle.odi.domain.model.OdiDataStore", "method": "getModel", "params": []},
        {"type": "oracle.odi.domain.model.OdiModel", "method": "getCode", "params": []},
        {"type": "oracle.odi.domain.model.OdiModel", "method": "getTechnology", "params": []},
        {"type": "oracle.odi.domain.model.finder.IOdiDataStoreFinder", "method": "findByModel", "params": [string]},
        {"type": "oracle.odi.domain.model.finder.IOdiModelFinder", "method": "findAll", "params": []},
        {"type": "oracle.odi.domain.project.finder.IOdiProjectFinder", "method": "findAll", "params": []},
        {"type": "oracle.odi.domain.mapping.Mapping", "method": "getAllComponents", "params": []},
        {"type": "oracle.odi.domain.mapping.component.DatastoreComponent", "method": "getBoundDataStore", "params": []},
        {"type": "oracle.odi.domain.mapping.component.DatastoreComponent", "method": "getAttributes", "params": []},
        {"type": "oracle.odi.domain.mapping.component.DatastoreComponent", "method": "getIntegrationType", "params": []},
        {"type": "oracle.odi.domain.mapping.MapAttribute", "method": "getExpression", "params": []},
        {"type": "oracle.odi.domain.mapping.MapAttribute", "method": "setExpressionText", "params": [string]},
        {"type": "oracle.odi.domain.mapping.MapAttribute", "method": "getExecuteOnHint", "params": []},
        {"type": "oracle.odi.domain.root.NamedObject", "method": "getName", "params": []},
        {"type": "oracle.odi.domain.mapping.expression.MapExpression", "method": "getText", "params": []},
        {"type": "oracle.odi.domain.runtime.session.finder.IOdiSessionFinder", "method": "countByStatus", "params": ["oracle.odi.domain.runtime.session.Status"]},
        {"type": "oracle.odi.core.persistence.IOdiEntityManager", "method": "flush", "params": []},
    ]

    assert fake.check_real_public_api(java, tmp_path, required) == {"missing": [], "script_imports_resolved": True}


# Python gate, receipt and normalizer behaviour, with an injected script callback.


class Script:
    """Stands in for the registered Groovy action and records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, parameters):
        self.calls.append(dict(parameters))
        return self.responses.pop(0)


def report_line(**overrides):
    payload = {
        "tool": repair.TOOL,
        "mode": repair.RAW_MODE,
        "action": "preflight",
        "status": "ready_for_repair",
        "code": None,
        "error_type": None,
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "repository_outcome": "no_write",
        "write_attempted": False,
        "commit_attempted": False,
        "commit_acknowledged": False,
        "rollback_attempted": True,
        "rollback_succeeded": True,
        "manual_recovery_review_required": False,
        "scenario_regeneration_required": False,
        "active_session_count": 0,
        "before_image": dict(ALL_NULL),
        "after_image": None,
        "changed_columns": [],
        "changed_expressions": [],
        "expression_profile": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


READY = report_line()
ALREADY = report_line(status="already_repaired", before_image=dict(ALL_OVERWRITE))


def repaired_line(**overrides):
    values = dict(
        action="repair", status="repaired", side_effects=True, repository_outcome="committed", write_attempted=True,
        commit_attempted=True, commit_acknowledged=True, rollback_attempted=False, rollback_succeeded=False,
        scenario_regeneration_required=True, after_image=dict(ALL_OVERWRITE), changed_columns=list(AUDIT_COLUMNS),
    )
    values.update(overrides)
    return report_line(**values)


def unknown_line(action="repair", **overrides):
    values = dict(
        action=action, status="outcome_unknown", error_type="RuntimeException", side_effects=None,
        repository_outcome="commit_outcome_unknown", write_attempted=True, commit_attempted=True,
        manual_recovery_review_required=True, scenario_regeneration_required=True,
    )
    values.update(overrides)
    return report_line(**values)


def blocked_line(action="repair", **overrides):
    values = dict(action=action, status="blocked", code="before_image_drift")
    values["repository_outcome"] = "rolled_back"
    values.update(overrides)
    return report_line(**values)


def restored_line(**overrides):
    values = dict(
        action="restore", status="restored", side_effects=True, repository_outcome="committed", write_attempted=True,
        commit_attempted=True, commit_acknowledged=True, rollback_attempted=False, rollback_succeeded=False,
        scenario_regeneration_required=True, before_image=dict(ALL_OVERWRITE), after_image=dict(ALL_NULL),
        changed_columns=list(AUDIT_COLUMNS),
    )
    values.update(overrides)
    return report_line(**values)


def act(action, script, directory, **overrides):
    values = dict(
        action=action,
        approved=True,
        approval_scope=SCOPES.get(action) if isinstance(action, str) else None,
        implementation_review_status="approved",
        no_active_odi_session_writes_attested=True,
        receipt_dir=directory,
        project="PRJ",
        folder="FLD",
        run_script=script,
    )
    values.update(overrides)
    return run_audit_role_action(**values)


def do_repair(tmp_path):
    script = Script(READY, repaired_line())
    return act("repair", script, tmp_path), script


@pytest.mark.parametrize("approved", [False, None, 1, "true", [], {}])
@pytest.mark.parametrize("action", list(SCOPES))
def test_wrong_approval_makes_zero_callback_calls(tmp_path, approved, action):
    script = Script()
    result = act(action, script, tmp_path, approved=approved)
    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False and script.calls == []


@pytest.mark.parametrize("scope", [None, "", "wrong", REPAIR_SCOPE + " ", REPAIR_SCOPE.upper(), PREFLIGHT_SCOPE, RESTORE_SCOPE])
def test_wrong_scope_makes_zero_callback_calls(tmp_path, scope):
    script = Script()
    result = act("repair", script, tmp_path, approval_scope=scope)
    assert result["status"] == "blocked_until_exact_approval_scope" and script.calls == []


def test_required_scope_strings_are_exact():
    assert REPAIR_SCOPE == "odi87_dim_product_audit_role_repair"
    assert STABLE_REPAIR_SCOPE == "odi87_dim_product_stable_audit_repair"
    assert STABLE_RESTORE_SCOPE == "odi87_dim_product_stable_audit_restore"
    assert RESTORE_SCOPE != REPAIR_SCOPE and PREFLIGHT_SCOPE != REPAIR_SCOPE
    assert STABLE_REPAIR_SCOPE != REPAIR_SCOPE and STABLE_RESTORE_SCOPE != RESTORE_SCOPE


@pytest.mark.parametrize("review", [None, "pending", True, "APPROVED"])
@pytest.mark.parametrize("action", ["repair", "restore", "stable_repair", "stable_restore"])
def test_unreviewed_implementation_makes_zero_callback_calls(tmp_path, action, review):
    script = Script()
    result = act(action, script, tmp_path, implementation_review_status=review)
    assert result["status"] == "blocked_until_implementation_review" and script.calls == []


@pytest.mark.parametrize("attested", [False, None, 1, "true"])
@pytest.mark.parametrize("action", ["repair", "restore", "stable_repair", "stable_restore"])
def test_missing_session_attestation_makes_zero_callback_calls(tmp_path, action, attested):
    script = Script()
    result = act(action, script, tmp_path, no_active_odi_session_writes_attested=attested)
    assert result["status"] == "blocked_until_session_attestation" and script.calls == []


@pytest.mark.parametrize("action", ["unknown", None, 1, ["repair"]])
def test_unknown_action_makes_zero_callback_calls(tmp_path, action):
    script = Script()
    assert act(action, script, tmp_path)["status"] == "blocked_invalid_action" and script.calls == []


@pytest.mark.parametrize("project", ["", "A B;", 'X"', None, 1])
def test_unsafe_project_or_folder_makes_zero_callback_calls(tmp_path, project):
    script = Script()
    assert act("preflight", script, tmp_path, project=project)["status"] == "blocked_invalid_project_context"
    assert act("preflight", script, tmp_path, folder=project)["status"] == "blocked_invalid_project_context"
    assert script.calls == []


@pytest.mark.parametrize("directory", [None, "relative/path", 1])
def test_unusable_receipt_directory_blocks_a_repair_before_any_call(directory):
    script = Script()
    result = act("repair", script, directory)
    assert result["status"] == "blocked_receipt_directory" and script.calls == []


def test_missing_receipt_directory_blocks_a_repair(tmp_path):
    script = Script()
    assert act("repair", script, tmp_path / "absent")["status"] == "blocked_receipt_directory" and script.calls == []


def test_repair_runs_a_fresh_preflight_and_writes_the_receipt_first(tmp_path):
    order = []

    def script(parameters):
        order.append((parameters["odi.audit.action"], (tmp_path / repair.RECEIPT_NAME).exists()))
        return READY if parameters["odi.audit.action"] == "preflight" else repaired_line()

    result = act("repair", script, tmp_path)
    assert order == [("preflight", False), ("repair", True)]
    assert result["status"] == "repaired" and result["receipt_written"] is True


def test_repair_passes_the_fresh_before_image_and_exact_scope(tmp_path):
    result, script = do_repair(tmp_path)
    assert script.calls[0]["odi.audit.approval_scope"] == PREFLIGHT_SCOPE
    second = script.calls[1]
    assert second["odi.audit.approval_scope"] == REPAIR_SCOPE and second["odi.audit.approved"] == "true"
    assert second["odi.audit.expected_before"] == repair.encode_image(ALL_NULL)
    assert second["odi.project"] == "PRJ" and second["odi.folder"] == "FLD"
    assert result["scenario_regeneration_required"] is True


def test_receipt_holds_only_role_metadata(tmp_path):
    do_repair(tmp_path)
    text = (tmp_path / repair.RECEIPT_NAME).read_text(encoding="utf-8")
    receipt = json.loads(text)
    assert set(receipt) == repair.RECEIPT_KEYS
    assert receipt["before_image"] == ALL_NULL and receipt["changed_columns"] == list(AUDIT_COLUMNS)
    assert LEAK_PATTERN.search(text) is None


def test_blocked_preflight_writes_no_receipt_and_never_repairs(tmp_path):
    script = Script(blocked_line("preflight", code="role_contract", before_image=None))
    result = act("repair", script, tmp_path)
    assert result["status"] == "blocked" and len(script.calls) == 1
    assert not (tmp_path / repair.RECEIPT_NAME).exists()


def test_already_repaired_writes_no_receipt_and_makes_no_write_call(tmp_path):
    script = Script(ALREADY)
    result = act("repair", script, tmp_path)
    assert result["status"] == "already_repaired" and len(script.calls) == 1
    assert not (tmp_path / repair.RECEIPT_NAME).exists()


def test_existing_receipt_is_never_overwritten_and_blocks_before_any_call(tmp_path):
    target = tmp_path / repair.RECEIPT_NAME
    target.write_text("EXISTING", encoding="utf-8")
    script = Script()
    result = act("repair", script, tmp_path)
    assert result["status"] == "blocked_receipt_exists" and script.calls == []
    assert target.read_text(encoding="utf-8") == "EXISTING"


def test_the_file_writer_itself_never_replaces_an_existing_file(tmp_path):
    target = tmp_path / "file.json"
    target.write_text("EXISTING", encoding="utf-8")
    assert repair._write_absent(target, {"schema_version": 1}) is False
    assert target.read_text(encoding="utf-8") == "EXISTING"


def test_a_report_with_a_duplicate_key_is_not_trusted(tmp_path):
    duplicated = repaired_line(side_effects=False)[:-1] + ', "side_effects": true}'
    result = act("repair", Script(READY, duplicated), tmp_path)
    assert result["status"] == "outcome_unknown" and result["side_effects"] is None


def test_unwritable_receipt_stops_before_the_repair_call(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("RAISE_TOKEN path")

    monkeypatch.setattr(repair.os, "open", fail)
    script = Script(READY)
    result = act("repair", script, tmp_path)
    assert result["status"] == "blocked_receipt_unwritable" and len(script.calls) == 1
    assert LEAK_PATTERN.search(json.dumps(result)) is None


def test_uncertain_repair_stays_unknown_and_is_recorded(tmp_path):
    script = Script(READY, unknown_line())
    result = act("repair", script, tmp_path)
    assert result["status"] == "outcome_unknown" and result["side_effects"] is None
    assert result["manual_recovery_review_required"] is True
    outcome = json.loads((tmp_path / repair.OUTCOME_NAME).read_text(encoding="utf-8"))
    assert outcome["repository_outcome"] == "commit_outcome_unknown"


@pytest.mark.parametrize("output", ["Hata: ODI Groovy action failed. error_type=TimeoutExpired", "", "not json", "{}", "[]", '{"status":"repaired"}'])
def test_unusable_repair_output_is_unknown_not_proof_of_absence(tmp_path, output):
    script = Script(READY, output)
    result = act("repair", script, tmp_path)
    assert result["status"] == "outcome_unknown" and result["side_effects"] is None
    assert result["manual_recovery_review_required"] is True
    assert result["repository_outcome"] == "unverified"


def test_a_launch_that_was_blocked_is_not_reported_as_unknown(tmp_path):
    script = Script(READY, "Hata: ODI Groovy action blocked. error_type=ApprovalRequired")
    result = act("repair", script, tmp_path)
    assert result["status"] == "blocked_not_launched" and result["side_effects"] is False


@pytest.mark.parametrize("output", ["Hata: ODI Groovy action failed. error_type=TimeoutExpired", "", "not json"])
def test_unusable_preflight_output_cannot_lead_to_a_write(tmp_path, output):
    script = Script(output)
    result = act("repair", script, tmp_path)
    assert result["status"] == "blocked_invalid_output" and result["side_effects"] is False
    assert len(script.calls) == 1 and not (tmp_path / repair.RECEIPT_NAME).exists()


def test_a_callback_exception_body_is_not_exposed(tmp_path):
    def script(parameters):
        if parameters["odi.audit.action"] == "preflight":
            return READY
        raise RuntimeError("RAISE_TOKEN jdbc")

    result = act("repair", script, tmp_path)
    assert result["status"] == "outcome_unknown" and result["side_effects"] is None
    assert result["error_type"] == "ScriptCallbackFailed" and LEAK_PATTERN.search(json.dumps(result)) is None


def test_failed_outcome_file_after_commit_keeps_the_commit(tmp_path, monkeypatch):
    real_open = repair.os.open

    def fail_outcome(path, *args, **kwargs):
        if str(path).endswith(repair.OUTCOME_NAME):
            raise OSError("RAISE_TOKEN")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(repair.os, "open", fail_outcome)
    result = act("repair", Script(READY, repaired_line()), tmp_path)
    assert result["status"] == "committed_report_incomplete"
    assert result["side_effects"] is True and result["manual_recovery_review_required"] is True
    assert result["repository_outcome"] == "committed"


def test_restore_requires_a_valid_receipt(tmp_path):
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_missing" and script.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(before_image={**ALL_NULL, "INSERT_DATE": "NATURAL_KEY"}),
        lambda r: r.update(changed_columns=["PROD_NAME"]),
        lambda r: r.update(changed_columns=[]),
        lambda r: r.update(scope="other"),
        lambda r: r.update(extra="value"),
        lambda r: r.pop("before_image"),
        lambda r: r.update(mapping="OTHER_MAPPING"),
        lambda r: r.update(target={"owner": "DWH", "table": "OTHER"}),
    ],
)
def test_a_tampered_receipt_blocks_restore_before_any_call(tmp_path, mutate):
    do_repair(tmp_path)
    path = tmp_path / repair.RECEIPT_NAME
    receipt = json.loads(path.read_text(encoding="utf-8"))
    mutate(receipt)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_invalid" and script.calls == []


def test_a_receipt_edited_into_a_consistent_shape_fails_the_digest(tmp_path):
    do_repair(tmp_path)
    path = tmp_path / repair.RECEIPT_NAME
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["before_image"]["INSERT_DATE"] = "OVERWRITE_ON_CHANGE"
    receipt["changed_columns"] = list(AUDIT_COLUMNS[1:])
    path.write_text(json.dumps(receipt), encoding="utf-8")
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_invalid" and script.calls == []


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"a":1,"a":2}'])
def test_an_unreadable_receipt_blocks_restore(tmp_path, text):
    (tmp_path / repair.RECEIPT_NAME).write_text(text, encoding="utf-8")
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_invalid" and script.calls == []


def test_restore_passes_exactly_what_the_receipt_proves(tmp_path):
    do_repair(tmp_path)
    script = Script(restored_line())
    result = act("restore", script, tmp_path)
    assert result["status"] == "restored"
    call = script.calls[0]
    assert call["odi.audit.action"] == "restore" and call["odi.audit.approval_scope"] == RESTORE_SCOPE
    assert call["odi.audit.expected_before"] == repair.encode_image(ALL_NULL)
    assert call["odi.audit.changed_columns"] == ",".join(AUDIT_COLUMNS)
    assert (tmp_path / repair.RESTORE_NAME).exists()


def test_a_used_receipt_is_stale_and_cannot_restore_twice(tmp_path):
    do_repair(tmp_path)
    act("restore", Script(restored_line()), tmp_path)
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_stale" and script.calls == []


def test_a_repair_that_rolled_back_proves_nothing_to_restore(tmp_path):
    act("repair", Script(READY, blocked_line()), tmp_path)
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_not_applied" and script.calls == []


def test_a_blocked_restore_writes_no_marker_and_can_be_retried(tmp_path):
    do_repair(tmp_path)
    result = act("restore", Script(blocked_line("restore", before_image=dict(ALL_OVERWRITE))), tmp_path)
    assert result["status"] == "blocked" and not (tmp_path / repair.RESTORE_NAME).exists()
    assert act("restore", Script(restored_line()), tmp_path)["status"] == "restored"


def test_an_unknown_repair_can_only_restore_through_a_state_check(tmp_path):
    act("repair", Script(READY, unknown_line()), tmp_path)
    script = Script(restored_line())
    assert act("restore", script, tmp_path)["status"] == "restored"
    assert script.calls[0]["odi.audit.changed_columns"] == ",".join(AUDIT_COLUMNS)


def test_uncertain_restore_stays_unknown_and_marks_the_receipt_used(tmp_path):
    do_repair(tmp_path)
    result = act("restore", Script(unknown_line("restore")), tmp_path)
    assert result["status"] == "outcome_unknown" and result["side_effects"] is None
    script = Script()
    assert act("restore", script, tmp_path)["status"] == "blocked_receipt_stale" and script.calls == []


def test_preflight_needs_no_receipt_or_attestation(tmp_path):
    script = Script(READY)
    result = act("preflight", script, None, implementation_review_status=None, no_active_odi_session_writes_attested=False)
    assert result["status"] == "ready_for_repair" and len(script.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_public_report_never_contains_raw_paths(tmp_path):
    result, _script = do_repair(tmp_path)
    assert str(tmp_path) not in json.dumps(result)


VALID_STATES = {
    "ready": READY,
    "already_repaired": ALREADY,
    "repaired": repaired_line(),
    "restored": restored_line(),
    "unknown_commit": unknown_line(),
    "unknown_commit_rollback_failed": unknown_line(rollback_succeeded=False),
    "unknown_rollback": unknown_line(repository_outcome="rollback_failed", commit_attempted=False, rollback_succeeded=False),
    "blocked": blocked_line(),
    "report_incomplete": repaired_line(status="committed_report_incomplete", manual_recovery_review_required=True, error_type="RuntimeException"),
    "aborted": blocked_line(status="aborted", code=None, error_type="RuntimeException", write_attempted=True),
    "aborted_no_transaction": blocked_line(status="aborted", code=None, error_type="RuntimeException", repository_outcome="no_transaction", rollback_attempted=False, rollback_succeeded=False),
}


@pytest.mark.parametrize("name", list(VALID_STATES))
def test_normalizer_preserves_every_truthful_state(name):
    raw = json.loads(VALID_STATES[name])
    result = normalize_audit_role_report(raw, raw["action"])
    for field in ("status", "side_effects", "repository_outcome", "commit_attempted", "commit_acknowledged", "rollback_attempted", "rollback_succeeded", "manual_recovery_review_required", "before_image"):
        assert result[field] == raw[field] and type(result[field]) is type(raw[field]), field


@pytest.mark.parametrize(
    ("base", "overrides"),
    [
        ("unknown_commit", {"side_effects": False}),
        ("unknown_commit", {"status": "blocked"}),
        ("unknown_commit", {"manual_recovery_review_required": False}),
        ("unknown_commit", {"commit_acknowledged": True}),
        ("unknown_rollback", {"side_effects": False}),
        ("unknown_rollback", {"rollback_succeeded": True}),
        ("repaired", {"side_effects": False}),
        ("repaired", {"commit_acknowledged": False}),
        ("repaired", {"scenario_regeneration_required": False}),
        ("repaired", {"manual_recovery_review_required": True}),
        ("repaired", {"after_image": {**ALL_OVERWRITE, "INSERT_DATE": "NULL"}}),
        ("repaired", {"after_image": None}),
        ("repaired", {"changed_columns": ["PROD_NAME"]}),
        ("repaired", {"changed_columns": []}),
        ("ready", {"side_effects": True}),
        ("ready", {"commit_attempted": True}),
        ("ready", {"before_image": None}),
        ("ready", {"before_image": {**ALL_NULL, "INSERT_DATE": "NATURAL_KEY"}}),
        ("ready", {"before_image": {**ALL_NULL, "PROD_NAME": "NULL"}}),
        ("ready", {"status": "already_repaired"}),
        ("already_repaired", {"status": "ready_for_repair"}),
        ("blocked", {"code": None}),
        ("blocked", {"code": "free text with detail"}),
        ("blocked", {"side_effects": True}),
        ("aborted", {"error_type": None}),
        ("aborted", {"error_type": "class with text"}),
        ("ready", {"tool": "other"}),
        ("ready", {"mode": "other"}),
        ("ready", {"raw_rows_included": True}),
        ("ready", {"pk_values_included": True}),
        ("ready", {"secret_printed": True}),
        ("ready", {"repository_connection_attempted": False}),
        ("ready", {"active_session_count": True}),
        ("ready", {"active_session_count": 1.0}),
        ("ready", {"expression": "EXPRESSION_TOKEN"}),
        ("ready", {"internal_id": 5}),
        ("repaired", {"action": "restore"}),
    ],
)
def test_normalizer_rejects_every_inconsistent_report(base, overrides):
    raw = json.loads(VALID_STATES[base])
    action = raw["action"]
    raw.update(overrides)
    result = normalize_audit_role_report(raw, action)
    assert result["status"] == "blocked_invalid_output", overrides
    assert LEAK_PATTERN.search(json.dumps(result)) is None


def test_orchestrator_wrapper_is_registered_and_strictly_approved(monkeypatch, tmp_path):
    import odi_orchestrator

    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[SCRIPT_NAME].category == "repository_write"
    orchestrator = odi_orchestrator.OdiOrchestrator()
    seen = []

    def fake_run(script_name, parameters):
        seen.append((script_name, dict(parameters), odi_orchestrator.groovy_is_approved()))
        return READY

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)
    blocked = orchestrator.dim_product_audit_role_action(action="preflight", approved="true", approval_scope=PREFLIGHT_SCOPE)
    assert blocked["status"] == "blocked_until_approval" and seen == []
    result = orchestrator.dim_product_audit_role_action(action="preflight", approved=True, approval_scope=PREFLIGHT_SCOPE)
    assert result["status"] == "ready_for_repair"
    script_name, parameters, approved = seen[0]
    assert script_name == SCRIPT_NAME and approved is True
    assert parameters["odi.project"] == odi_orchestrator.ODI_PROJECT
    assert parameters["odi.folder"] == odi_orchestrator.ODI_FOLDER
    assert odi_orchestrator.groovy_is_approved() is False


def test_real_run_groovy_passes_every_report_status_through(monkeypatch):
    """Runs the real boundary with a fake process, so the output filter cannot lose a report."""
    import subprocess

    import odi_orchestrator

    orchestrator = odi_orchestrator.OdiOrchestrator()
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: "SYNTHETIC_NOT_READ.properties")
    for name, line in VALID_STATES.items():
        monkeypatch.setattr(
            subprocess, "run",
            lambda *_a, line=line, **_k: type("Done", (), {"stdout": line, "stderr": "", "returncode": 0})(),
        )
        with odi_orchestrator.groovy_approval_context(True):
            output = orchestrator.run_groovy(SCRIPT_NAME, {"odi.audit.action": "preflight"})
        assert json.loads(output) == json.loads(line), name


def test_real_run_groovy_blocks_without_the_approval_context():
    import odi_orchestrator

    output = odi_orchestrator.OdiOrchestrator().run_groovy(SCRIPT_NAME, {"odi.audit.action": "preflight"})
    assert output.startswith("Hata: ODI Groovy action blocked")


def sqlite_audit_sql(oracle_sql: str, batch: int) -> str:
    """Translate only DECODE and the session-batch token. The CASE shape stays."""
    sql = oracle_sql.replace('<%=odiRef.getSession("SESS_NO")%>', str(batch))
    sql = re.sub(
        r"DECODE\(([A-Z0-9_.]+), ([A-Z0-9_.]+), 0, 1\)",
        r"(CASE WHEN \1 IS \2 THEN 0 ELSE 1 END)",
        sql,
    )
    assert "DECODE" not in sql and "odiRef" not in sql and "ROWNUM" not in sql
    return sql


def _business_row(contract: dict, **overrides: object) -> dict:
    row: dict = {contract["natural_key"]: 101}
    for name, meta in contract["tracked_types"].items():
        if meta["nullable"]:
            row[name] = None
        elif "VARCHAR" in str(meta["data_type"]):
            row[name] = "T"
        else:
            row[name] = 1
    row.update(overrides)
    return row


def _evaluate_audits(source: dict, dim_rows: list[dict], batch: int, alias: str = "ODS_SH_PRODUCTS") -> dict:
    import sqlite3

    contract = expected_contract()
    expressions = {column: stable_audit_expression(column, alias) for column in AUDIT_COLUMNS}
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS DWH")
    columns = [contract["natural_key"], *contract["tracked"], "CURRENT_FLAG", *AUDIT_COLUMNS]
    connection.execute(f"CREATE TABLE DWH.DIM_PRODUCT ({', '.join(columns)})")
    for row in dim_rows:
        connection.execute(
            f"INSERT INTO DWH.DIM_PRODUCT VALUES ({', '.join('?' for _ in columns)})",
            [row.get(column) for column in columns],
        )
    # Oracle raises ORA-01427 when this scalar matches two rows. SQLite 3.49 returns
    # one row instead, so the adaptor rejects that count before accepting a value.
    matched = connection.execute(
        "SELECT COUNT(*) FROM DWH.DIM_PRODUCT C "
        "WHERE C.PROD_ID = ? AND C.CURRENT_FLAG = '1'",
        (source[contract["natural_key"]],),
    ).fetchone()[0]
    if matched > 1:
        connection.close()
        raise sqlite3.OperationalError("scalar subquery matched more than one current row")
    selected = ", ".join(f"({sqlite_audit_sql(sql, batch)}) AS {column}" for column, sql in expressions.items())
    projected = ", ".join(f"? AS {column}" for column in (contract["natural_key"], *contract["tracked"]))
    cursor = connection.execute(
        f"SELECT {selected} FROM (SELECT {projected}) {alias}",
        [source[column] for column in (contract["natural_key"], *contract["tracked"])],
    )
    values = dict(zip(AUDIT_COLUMNS, cursor.fetchone(), strict=True))
    connection.close()
    return values


def test_tracked_metadata_and_frozen_expressions_come_from_the_planner():
    from scd2_manager import PRODUCTS_SOURCE_METADATA

    contract = expected_contract()
    assert len(contract["tracked"]) == 17
    assert len(contract["fixed_expressions"]) == 22
    nullable = [name for name, meta in contract["tracked_types"].items() if meta["nullable"]]
    assert nullable == ["PROD_UNIT_OF_MEASURE"]
    source_types = {name: data_type for name, data_type, _nullable in PRODUCTS_SOURCE_METADATA}
    for name, meta in contract["tracked_types"].items():
        assert meta["data_type"] == source_types[name]
    alias = "ODS_SH_PRODUCTS"
    for column in (contract["natural_key"], *contract["tracked"]):
        assert contract["fixed_expressions"][column] == f"{alias}.{column}"
    assert fixed_expression_issue("CURRENT_FLAG", "Y", alias) == "legacy_expression_gap"
    assert fixed_expression_issue("EFFECTIVE_TO", "DATE '9999-12-31'", alias) == "legacy_expression_gap"
    assert fixed_expression_issue("EFFECTIVE_TO", "DATE '1999-01-01'", alias) == "fixed_expression_drift"
    assert fixed_expression_issue("CURRENT_FLAG", "1", alias) is None
    for column in AUDIT_COLUMNS:
        sql = stable_audit_expression(column, alias)
        assert " JOIN " not in sql and "ROWNUM" not in sql
        assert "C.CURRENT_FLAG = '1'" in sql
        assert sql.endswith(f"ELSE {contract['audit_expressions'][column]} END")
        for tracked in contract["tracked"]:
            assert f"DECODE(C.{tracked}, {alias}.{tracked}, 0, 1) = 0" in sql


def test_case_preserves_audits_for_equal_and_null_business_rows():
    contract = expected_contract()
    current = {
        **_business_row(contract),
        "CURRENT_FLAG": "1",
        "INSERT_DATE": "2020-01-01 00:00:00",
        "UPDATE_DATE": "2020-06-01 00:00:00",
        "SOURCE_SYSTEM": "SH",
        "ETL_BATCH_ID": 7,
    }
    history = {
        **current,
        "CURRENT_FLAG": "0",
        "INSERT_DATE": "2019-01-01 00:00:00",
        "UPDATE_DATE": "2019-06-01 00:00:00",
        "ETL_BATCH_ID": 3,
    }
    preserved = _evaluate_audits(dict(current), [history, current], 99)
    assert preserved == {
        "INSERT_DATE": current["INSERT_DATE"],
        "UPDATE_DATE": current["UPDATE_DATE"],
        "SOURCE_SYSTEM": "SH",
        "ETL_BATCH_ID": 7,
    }
    again = _evaluate_audits(dict(current), [history, current], 250)
    assert again == preserved


@pytest.mark.parametrize("column", list(expected_contract()["tracked"]))
def test_each_tracked_change_refreshes_audits_and_equality_preserves_them(column):
    contract = expected_contract()
    current = {
        **_business_row(contract),
        "CURRENT_FLAG": "1",
        "INSERT_DATE": "2020-01-01 00:00:00",
        "UPDATE_DATE": "2020-06-01 00:00:00",
        "SOURCE_SYSTEM": "SH",
        "ETL_BATCH_ID": 7,
    }
    same = _evaluate_audits(dict(current), [dict(current)], 40)
    assert same["INSERT_DATE"] == current["INSERT_DATE"] and same["ETL_BATCH_ID"] == 7
    changed_source = dict(current)
    changed_source[column] = "CHANGED" if current[column] is None or isinstance(current[column], str) else 9
    refreshed = _evaluate_audits(changed_source, [dict(current)], 40)
    assert refreshed["SOURCE_SYSTEM"] == "SH"
    assert refreshed["ETL_BATCH_ID"] == 40
    assert refreshed["INSERT_DATE"] == refreshed["UPDATE_DATE"]
    assert refreshed["INSERT_DATE"] != current["INSERT_DATE"]


def test_new_natural_key_uses_the_creation_expression():
    contract = expected_contract()
    current = {**_business_row(contract), "CURRENT_FLAG": "1", "INSERT_DATE": "2020-01-01 00:00:00",
               "UPDATE_DATE": "2020-06-01 00:00:00", "SOURCE_SYSTEM": "SH", "ETL_BATCH_ID": 7}
    source = dict(current)
    source[contract["natural_key"]] = 104
    created = _evaluate_audits(source, [current], 15)
    assert created["SOURCE_SYSTEM"] == "SH" and created["ETL_BATCH_ID"] == 15
    assert created["INSERT_DATE"] == created["UPDATE_DATE"]
    assert created["INSERT_DATE"] != current["INSERT_DATE"]


def test_two_current_rows_make_the_scalar_query_fail():
    import sqlite3

    contract = expected_contract()
    current = {**_business_row(contract), "CURRENT_FLAG": "1", "INSERT_DATE": "2020-01-01 00:00:00",
               "UPDATE_DATE": "2020-06-01 00:00:00", "SOURCE_SYSTEM": "SH", "ETL_BATCH_ID": 7}
    with pytest.raises(sqlite3.Error):
        _evaluate_audits(dict(current), [dict(current), dict(current)], 15)


def test_existing_readiness_rejects_multiple_current_rows_before_load():
    from dim_product_scd2_candidate_preflight import candidate_status

    evidence = {
        "source_duplicate_groups": 0,
        "duplicate_product_sk_groups": 0,
        "duplicate_current_natural_key_groups": 1,
        "invalid_current_flags": 0,
        "invalid_effective_dates": 0,
        "source_missing_current_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "insert_without_current_count": 0,
        "changed_current_count": 0,
        "unchanged_current_count": 1,
        "source_count": 1,
    }
    assert candidate_status(evidence) == "blocked_target_multiple_current_rows"


def test_role_scope_cannot_authorize_an_expression_write(tmp_path):
    script = Script()
    result = act("stable_repair", script, tmp_path, approval_scope=REPAIR_SCOPE)
    assert result["status"] == "blocked_until_exact_approval_scope" and script.calls == []


def test_stable_repair_records_profile_ids_without_sql(tmp_path):
    def run(parameters):
        if parameters["odi.audit.action"] == "stable_preflight":
            return report_line(action="stable_preflight", expression_profile=VOLATILE_PROFILE_ID)
        assert (tmp_path / repair.RECEIPT_NAME).is_file()
        return report_line(
            action="stable_repair",
            status="repaired",
            side_effects=True,
            repository_outcome="committed",
            write_attempted=True,
            commit_attempted=True,
            commit_acknowledged=True,
            rollback_attempted=False,
            rollback_succeeded=False,
            scenario_regeneration_required=True,
            after_image=dict(ALL_OVERWRITE),
            changed_columns=list(AUDIT_COLUMNS),
            changed_expressions=list(AUDIT_COLUMNS),
            expression_profile=STABLE_PROFILE_ID,
        )

    result = act("stable_repair", run, tmp_path)
    assert result["status"] == "repaired"
    text = (tmp_path / repair.RECEIPT_NAME).read_text(encoding="utf-8")
    receipt = json.loads(text)
    assert receipt["schema_version"] == 2
    assert receipt["expression_profile_before"] == repair.profile_record(VOLATILE_PROFILE_ID)
    assert receipt["expression_profile_target"]["id"] == STABLE_PROFILE_ID
    assert "CURRENT_TIMESTAMP" not in text and "DECODE" not in text and "SELECT" not in text
    assert "CASE" not in json.dumps(result)


def test_already_stable_does_not_write_a_new_receipt(tmp_path):
    script = Script(report_line(
        action="stable_preflight",
        status="already_repaired",
        before_image=dict(ALL_OVERWRITE),
        expression_profile=STABLE_PROFILE_ID,
    ))
    result = act("stable_repair", script, tmp_path)
    assert result["status"] == "already_repaired" and len(script.calls) == 1
    assert not (tmp_path / repair.RECEIPT_NAME).exists()


def test_unknown_profile_receipt_is_rejected_without_a_call_or_sql(tmp_path):
    payload = {
        "schema_version": 2,
        "sql": "SELECT SECRET FROM DWH.DIM_PRODUCT",
        "expression_profile_before": {"id": "unknown", "hash": "0"},
    }
    (tmp_path / repair.RECEIPT_NAME).write_text(json.dumps(payload), encoding="utf-8")
    script = Script()
    result = act("stable_restore", script, tmp_path)
    assert result["status"] == "blocked_receipt_invalid" and script.calls == []
    rendered = json.dumps(result)
    assert "SELECT" not in rendered and "SECRET" not in rendered and "DECODE" not in rendered


def test_role_receipt_cannot_restore_expressions(tmp_path):
    do_repair(tmp_path)
    script = Script()
    result = act("stable_restore", script, tmp_path)
    assert result["status"] == "blocked_receipt_invalid" and script.calls == []


def test_stable_script_writes_expressions_and_keeps_hints(runs):
    run = runs["stable_repair_all"]
    payload = payload_of(run)
    assert payload["status"] == "repaired"
    assert payload["expression_profile"] == STABLE_PROFILE_ID
    assert payload["changed_expressions"] == list(AUDIT_COLUMNS)
    assert not any(event.startswith("setExecuteOnHint") for event in run["trace"])
    for column in AUDIT_COLUMNS:
        assert run["expressions"][column] == stable_audit_expression(column, "ODS_SH_PRODUCTS")
    assert run["hints"]["PRODUCT_SK"] == "TARGET"
    assert run["hints"]["INSERT_DATE"] == "NO_HINT"


def test_role_repair_does_not_rewrite_audit_expressions(runs):
    run = runs["repair_all"]
    assert not any(event.startswith("setExpressionText") for event in run["trace"])
    for column, text in expected_contract()["audit_expressions"].items():
        assert run["expressions"][column] == text


def test_rebound_alias_is_the_one_written_into_the_expression(runs):
    run = runs["stable_repair_rebind_alias"]
    written = run["expressions"]["ETL_BATCH_ID"]
    assert written == stable_audit_expression("ETL_BATCH_ID", "SRC_ALIAS")
    assert "ODS_SH_PRODUCTS" not in written


def test_stable_restore_returns_the_volatile_profile(runs):
    run = runs["stable_restore_to_volatile"]
    assert payload_of(run)["status"] == "restored"
    for column, text in expected_contract()["audit_expressions"].items():
        assert run["expressions"][column] == text
    assert all(run["persisted"][column] is None for column in AUDIT_COLUMNS)


def test_expression_failure_rolls_back_roles_and_expressions(runs):
    run = runs["stable_expression_setter_raises"]
    payload = payload_of(run)
    assert payload["status"] == "aborted" and payload["error_type"] == "RuntimeException"
    assert payload["repository_outcome"] == "rolled_back" and payload["side_effects"] is False
    assert run["committed"] is False and "commit" not in run["trace"]
    assert all(run["persisted"][column] is None for column in AUDIT_COLUMNS)
    assert run["expressions"]["INSERT_DATE"] == expected_contract()["audit_expressions"]["INSERT_DATE"]


def test_stable_commit_loss_and_rollback_failure_stay_unknown(runs):
    applied = payload_of(runs["stable_commit_applied_then_raises"])
    assert applied["status"] == "outcome_unknown" and applied["repository_outcome"] == "commit_outcome_unknown"
    assert applied["side_effects"] is None and runs["stable_commit_applied_then_raises"]["committed"] is True
    failed = payload_of(runs["stable_rollback_fails"])
    assert failed["repository_outcome"] == "rollback_failed" and failed["side_effects"] is None


def test_legacy_and_hint_drift_block_before_any_expression_write(runs):
    expected = {
        "legacy_flag": "legacy_expression_gap",
        "legacy_end": "legacy_expression_gap",
        "unknown_end": "fixed_expression_drift",
        "wrong_tracked_expression": "fixed_expression_drift",
        "audit_hint_target": "audit_execute_hint_target",
        "sk_hint_missing": "execute_hint_drift",
        "bad_source_alias": "source_alias_invalid",
    }
    for name, code in expected.items():
        run = runs[name]
        assert payload_of(run)["code"] == code
        assert not any(event.startswith("setExpressionText") for event in run["trace"])
        assert "commit" not in run["trace"]


def test_unknown_profile_argument_is_not_echoed(runs):
    run = runs["script_stable_profile_sql"]
    assert "begin" not in run["trace"]
    assert "SELECT" not in run["output"] and "DECODE" not in run["output"] and "SECRET" not in run["output"]
