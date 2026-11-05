from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAME = "dim_product_scd2_mapping_create.groovy"
APPROVAL_SCOPE = "odi68_dim_product_odi_scd2_mapping_creation"


def _readiness(**overrides):
    report = {
        "status": "ready_for_separate_mapping_builder_implementation_review",
        "source": {"owner": "DWH", "table": "ODS_SH_PRODUCTS"},
        "target": {"owner": "DWH", "table": "DIM_PRODUCT"},
        "mapping": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
        "scenario": {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN", "version": "001"},
        "required_target_ikm": "IKM Oracle Slowly Changing Dimension",
        "required_lkm": "LKM SQL to Oracle (Built-In)",
        "server_side_executor_remains_baseline": True,
        "mapping_collision_free": True,
        "scenario_collision_free": True,
        "target_ikm_verified": True,
        "scd2_roles_verified": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    report.update(overrides)
    return report


def _raw_success():
    return {
        "tool": "odi.dim_product_scd2_mapping_create",
        "status": "passed",
        "mode": "fixed_absent_only_scd2_mapping_creation",
        "side_effects": True,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "result": {
            "status": "passed",
            "mapping_name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
            "source_component_count": 1,
            "target_component_count": 1,
            "target_attribute_count": 26,
            "mapped_attribute_count": 26,
            "integration_type": "SCD",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "access_point_count": 0,
            "lkm_assigned_count": 0,
            "commit_completed": True,
        },
    }


def _request(**overrides):
    from dim_product_odi_scd2_mapping_builder import (
        build_dim_product_odi_scd2_mapping_request,
    )

    values = {
        "packaging_readiness": _readiness(),
        "mapping_contract_review_status": "approved",
        "mapping_builder_implementation_review_status": "approved",
        "approved": True,
        "approval_scope": APPROVAL_SCOPE,
    }
    values.update(overrides)
    return build_dim_product_odi_scd2_mapping_request(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"packaging_readiness": _readiness(status="blocked")}, "blocked_packaging_readiness"),
        ({"mapping_contract_review_status": "pending"}, "blocked_mapping_contract_review"),
        ({"mapping_builder_implementation_review_status": "pending"}, "blocked_mapping_builder_review"),
        ({"approved": "yes"}, "blocked_until_explicit_approval"),
        ({"approved": 1}, "blocked_until_explicit_approval"),
        ({"approval_scope": "wrong"}, "blocked_until_exact_approval_scope"),
    ],
)
def test_mapping_request_fails_closed_before_repository_connection(overrides, expected):
    result = _request(**overrides)

    assert result["status"] == expected
    assert result["connection_attempted"] is False
    assert result["write_operations_attempted"] == []


def test_mapping_normalizer_accepts_only_fixed_count_status_metadata():
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    result = normalize_dim_product_odi_scd2_mapping(_raw_success())

    assert result["status"] == "passed"
    assert result["result"]["mapped_attribute_count"] == 26
    assert result["result"]["integration_type"] == "SCD"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.update({"unexpected": "repository-id"}),
        lambda p: p["result"].update({"expression": "not-allowed"}),
        lambda p: p["result"].update({"commit_completed": 1}),
    ],
)
def test_mapping_normalizer_rejects_extra_or_weak_fields(mutator):
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    payload = deepcopy(_raw_success())
    mutator(payload)
    result = normalize_dim_product_odi_scd2_mapping(payload)

    assert result["status"] == "blocked_invalid_mapping_output"
    assert "repository-id" not in json.dumps(result)
    assert "not-allowed" not in json.dumps(result)
    assert result["side_effects"] is None
    assert result["repository_outcome"] == "unverified"
    assert result["manual_recovery_review_required"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_component_count", True),
        ("source_component_count", 1.0),
        ("source_component_count", "1"),
        ("target_component_count", True),
        ("target_component_count", 1.0),
        ("target_attribute_count", 26.0),
        ("mapped_attribute_count", 26.0),
        ("access_point_count", True),
        ("lkm_assigned_count", True),
        ("access_point_count", 1),
        ("lkm_assigned_count", 1),
        ("access_point_count", -1),
    ],
)
def test_mapping_normalizer_requires_literal_counts_and_equal_lkm_cardinality(field, value):
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    payload = _raw_success()
    payload["result"][field] = value

    assert normalize_dim_product_odi_scd2_mapping(payload)["status"] == "blocked_invalid_mapping_output"


@pytest.mark.parametrize(("access_points", "assigned"), [(True, True), (True, 1), (1, True), (1.0, 1.0)])
def test_mapping_normalizer_rejects_equal_lkm_counts_that_are_not_integers(access_points, assigned):
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    payload = _raw_success()
    payload["result"].update({"access_point_count": access_points, "lkm_assigned_count": assigned})

    assert normalize_dim_product_odi_scd2_mapping(payload)["status"] == "blocked_invalid_mapping_output"


def test_mapping_normalizer_accepts_equal_nonzero_lkm_cardinality():
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    payload = _raw_success()
    payload["result"].update({"access_point_count": 2, "lkm_assigned_count": 2})

    assert normalize_dim_product_odi_scd2_mapping(payload)["status"] == "passed"


def _raw_failure(**overrides):
    payload = {
        "tool": "odi.dim_product_scd2_mapping_create",
        "status": "error",
        "mode": "fixed_absent_only_scd2_mapping_creation",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "error_type": "IllegalStateException",
        "repository_outcome": "rolled_back",
        "commit_attempted": False,
        "commit_acknowledged": False,
        "rollback_attempted": True,
        "rollback_succeeded": True,
        "manual_recovery_review_required": False,
    }
    payload.update(overrides)
    return payload


_COMMIT_UNKNOWN = {
    "status": "outcome_unknown",
    "side_effects": None,
    "repository_outcome": "commit_outcome_unknown",
    "commit_attempted": True,
    "manual_recovery_review_required": True,
}
_ROLLBACK_FAILED = {
    "status": "outcome_unknown",
    "side_effects": None,
    "repository_outcome": "rollback_failed",
    "rollback_succeeded": False,
    "manual_recovery_review_required": True,
}
_NO_TRANSACTION = {"repository_outcome": "no_transaction", "rollback_attempted": False, "rollback_succeeded": False}
_COMMITTED = {
    "status": "committed_report_incomplete",
    "side_effects": True,
    "repository_outcome": "committed",
    "commit_attempted": True,
    "commit_acknowledged": True,
    "rollback_attempted": False,
    "rollback_succeeded": False,
    "manual_recovery_review_required": True,
}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        _NO_TRANSACTION,
        _COMMIT_UNKNOWN,
        {**_COMMIT_UNKNOWN, "rollback_succeeded": False},
        _ROLLBACK_FAILED,
        _COMMITTED,
    ],
)
def test_mapping_normalizer_preserves_every_truthful_failure_state(overrides):
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    payload = _raw_failure(**overrides)
    result = normalize_dim_product_odi_scd2_mapping(payload)

    for field in (
        "status",
        "side_effects",
        "error_type",
        "repository_outcome",
        "commit_attempted",
        "commit_acknowledged",
        "rollback_attempted",
        "rollback_succeeded",
        "manual_recovery_review_required",
    ):
        assert result[field] == payload[field], field
        assert type(result[field]) is type(payload[field]), field
    assert "result" not in result


@pytest.mark.parametrize(
    "overrides",
    [
        # A successful rollback or a false flag cannot clear an uncertain commit.
        {**_COMMIT_UNKNOWN, "side_effects": False},
        {**_COMMIT_UNKNOWN, "status": "error"},
        {**_COMMIT_UNKNOWN, "manual_recovery_review_required": False},
        {**_COMMIT_UNKNOWN, "commit_attempted": False},
        {**_COMMIT_UNKNOWN, "commit_acknowledged": True},
        {**_COMMIT_UNKNOWN, "rollback_attempted": False},
        # A failed rollback cannot claim a clean no-effect result.
        {**_ROLLBACK_FAILED, "side_effects": False},
        {**_ROLLBACK_FAILED, "status": "error"},
        {**_ROLLBACK_FAILED, "manual_recovery_review_required": False},
        {**_ROLLBACK_FAILED, "rollback_succeeded": True},
        # A clean error cannot carry any trace of a commit or a failed rollback.
        {"commit_attempted": True},
        {"commit_acknowledged": True},
        {"rollback_succeeded": False},
        {"rollback_attempted": False},
        {"manual_recovery_review_required": True},
        {"side_effects": None},
        {"side_effects": True},
        {**_NO_TRANSACTION, "rollback_attempted": True},
        # An acknowledged commit cannot be reported as anything else.
        {**_COMMITTED, "side_effects": False},
        {**_COMMITTED, "status": "error"},
        {**_COMMITTED, "commit_acknowledged": False},
        {**_COMMITTED, "manual_recovery_review_required": False},
        # Weak types and unknown labels.
        {"repository_outcome": "clean"},
        {"repository_outcome": None},
        {"repository_outcome": ["rolled_back"]},
        {"commit_attempted": 0},
        {"rollback_succeeded": 1},
        {"manual_recovery_review_required": 0},
        {"error_type": "class with text"},
        {"error_type": None},
        {"status": "unknown"},
        {"tool": "other"},
        {"mode": "other"},
        {"repository_connection_attempted": False},
        {"raw_rows_included": True},
        {"pk_values_included": True},
        {"secret_printed": True},
        {"error": "repository-id"},
    ],
)
def test_malformed_failure_reports_never_become_proof_of_absence(overrides):
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    result = normalize_dim_product_odi_scd2_mapping(_raw_failure(**overrides))

    assert result["status"] == "blocked_invalid_mapping_output"
    assert result["side_effects"] is None
    assert result["repository_outcome"] == "unverified"
    assert result["manual_recovery_review_required"] is True
    assert "repository-id" not in json.dumps(result)
    assert "class with text" not in json.dumps(result)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {"status": "error"},
        {
            # The envelope the writer used before this change, with no outcome fields.
            "tool": "odi.dim_product_scd2_mapping_create",
            "status": "error",
            "mode": "fixed_absent_only_scd2_mapping_creation",
            "side_effects": False,
            "repository_connection_attempted": True,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "error_type": "RuntimeException",
        },
    ],
)
def test_unparseable_or_old_writer_output_stays_unverified(payload):
    from dim_product_odi_scd2_mapping_builder import normalize_dim_product_odi_scd2_mapping

    result = normalize_dim_product_odi_scd2_mapping(payload)

    assert result["status"] == "blocked_invalid_mapping_output"
    assert result["side_effects"] is None
    assert result["manual_recovery_review_required"] is True


def test_dedicated_mapping_script_is_fixed_scd_public_api_and_non_destructive():
    import odi_orchestrator

    script = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(encoding="utf-8")
    lowered = script.lower()

    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[SCRIPT_NAME].category == "repository_write"
    for required in (
        'final String MAPPING_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"',
        'final String SOURCE_TABLE = "ODS_SH_PRODUCTS"',
        'final String TARGET_TABLE = "DIM_PRODUCT"',
        "INTEGRATION_TYPE_SCD",
        "setTargetComponentKM",
        "setLKM",
        "SEQ_DIM_PRODUCT.NEXTVAL",
        "ExecuteOnLocation.TARGET",
        "mapping already exists",
    ):
        assert required in script
    for forbidden in (
        "replace_existing",
        ".remove(",
        "delete ",
        "drop ",
        "truncate ",
        "setaccessible",
        "getdeclaredfield",
        "printstacktrace",
    ):
        assert forbidden not in lowered


def test_mapping_script_text_keeps_the_gate_calls_where_no_java_runtime_exists():
    """Static fallback for CI. Behaviour is in test_dim_product_scd2_mapping_create_fake_sdk.py."""
    script = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(encoding="utf-8")

    for required in (
        "mapping.validate(finalIssues, false)",
        "RootIssue.isErrorOrFatal(finalIssues)",
        "design.getAllAPNodes()",
        "node.setLKM(lkm)",
        "getLKMName()",
        "getIKMOptionValue(optionName)",
        "candidates.size() != 1",
        "commitAttempted = true",
        "commitAcknowledged = true",
        'repositoryOutcome = "commit_outcome_unknown"',
        'repositoryOutcome = "rollback_failed"',
    ):
        assert required in script
    for forbidden in ("setXKM", "getXKMName", "mapping.validate()", "setCheckKM", "setScdType"):
        assert forbidden not in script


def test_orchestrator_mapping_wrapper_uses_fixed_script_without_object_params(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []

    def fake_run(script, params):
        calls.append((script, params, odi_orchestrator._GROOVY_APPROVED.get()))
        return json.dumps(_raw_success())

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)
    result = orchestrator.dim_product_scd2_mapping_create(
        packaging_readiness=_readiness(),
        mapping_contract_review_status="approved",
        mapping_builder_implementation_review_status="approved",
        approved=True,
        approval_scope=APPROVAL_SCOPE,
    )

    assert result["status"] == "passed"
    assert calls == [(SCRIPT_NAME, {"odi.project": odi_orchestrator.ODI_PROJECT, "odi.folder": odi_orchestrator.ODI_FOLDER}, True)]

