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

