from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


def _readiness(**overrides):
    report = {
        "status": "ready_for_separate_mapping_builder_implementation_review",
        "source": {"owner": "DWH", "table": "ODS_SH_PRODUCTS"},
        "target": {"owner": "DWH", "table": "DIM_PRODUCT"},
        "mapping": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
        "scenario": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
            "version": "001",
        },
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


def _request(**overrides):
    from dim_product_odi_scd2_mapping_builder_contract import (
        APPROVAL_SCOPE,
        build_dim_product_odi_scd2_mapping_builder_request,
    )

    values = {
        "packaging_readiness": _readiness(),
        "capability_probe_review_status": "approved",
        "mapping_builder_contract_review_status": "approved",
        "approved": True,
        "approval_scope": APPROVAL_SCOPE,
    }
    values.update(overrides)
    return build_dim_product_odi_scd2_mapping_builder_request(**values)


def test_static_builder_plan_is_absent_only_scd2_and_not_generic_reuse():
    from dim_product_odi_scd2_mapping_builder_contract import (
        APPROVAL_SCOPE,
        build_dim_product_odi_scd2_mapping_builder_plan,
    )

    plan = build_dim_product_odi_scd2_mapping_builder_plan()

    assert plan["source"] == {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
    assert plan["target"] == {"owner": "DWH", "table": "DIM_PRODUCT"}
    assert plan["mapping"] == "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
    assert plan["required_target_ikm"] == "IKM Oracle Slowly Changing Dimension"
    assert plan["required_lkm"] == "LKM SQL to Oracle (Built-In)"
    assert plan["approval_scope"] == APPROVAL_SCOPE
    assert plan["existing_generic_builder_reusable"] is False
    assert plan["absent_only_mapping_creation"] is True
    assert plan["mapping_replacement_allowed"] is False
    assert plan["scenario_generation_included"] is False
    assert plan["scenario_run_included"] is False
    assert plan["server_side_executor_remains_baseline"] is True
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"packaging_readiness": _readiness(status="blocked_capability_probe")},
            "blocked_until_packaging_readiness",
        ),
        (
            {"capability_probe_review_status": "pending"},
            "blocked_until_capability_probe_review",
        ),
        (
            {"mapping_builder_contract_review_status": "pending"},
            "blocked_until_mapping_builder_contract_review",
        ),
        ({"approved": "yes"}, "blocked_until_explicit_approval"),
        ({"approved": 1}, "blocked_until_explicit_approval"),
        ({"approval_scope": "wrong_scope"}, "blocked_until_exact_approval_scope"),
    ],
)
def test_builder_request_fails_closed_before_any_future_executor(overrides, expected):
    result = _request(**overrides)

    assert result["status"] == expected
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False
    assert result["write_operations_attempted"] == []


def test_all_gates_still_stop_at_separate_builder_implementation():
    result = _request()

    assert result["status"] == "blocked_pending_separate_mapping_builder_implementation"
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False
    assert result["write_operations_attempted"] == []
    assert result["scenario_generation_included"] is False
    assert result["scenario_run_included"] is False


def test_builder_contract_and_runbook_have_no_connection_or_execution_surface():
    module = (
        PROJECT_ROOT / "dim_product_odi_scd2_mapping_builder_contract.py"
    ).read_text(encoding="utf-8")
    runbook = (
        PROJECT_ROOT / "docs" / "dwh-dim-product-odi-scd2-mapping-builder-contract.md"
    ).read_text(encoding="utf-8")
    combined = f"{module}\n{runbook}".lower()

    for forbidden in (
        "oracledb",
        "connect(",
        "run_groovy",
        "subprocess",
        "from config",
        "import config",
        "@app.route",
        "blueprint",
        "generate_scenario(",
        "startscenario(",
        "insert into",
        "update dwh.",
        "delete from",
        "drop ",
        "truncate",
    ):
        assert forbidden not in combined

    assert "does not authorize mapping creation" in runbook.lower()
    assert "separate review" in runbook.lower()
    assert "no raw rows" in runbook.lower()

