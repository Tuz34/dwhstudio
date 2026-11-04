from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mapping_inspection(**overrides):
    report = {
        "status": "passed",
        "side_effects": False,
        "physical": {
            "target_ikm": "IKM Oracle Merge",
            "lkm": "LKM SQL to Oracle (Built-In)",
        },
        "logical": {
            "integration_type": "INCREMENTAL_UPDATE",
            "update_key": "UK_ODS_SH_PRODUCTS_PROD_ID",
            "update_key_columns": ["PROD_ID"],
            "audit_expressions": {
                "ODS_CREATED_AT": "CURRENT_TIMESTAMP",
                "ODS_UPDATED_AT": "CURRENT_TIMESTAMP",
            },
        },
    }
    report.update(overrides)
    return report


def _absent_scenario_probe():
    return {
        "status": "attention_required",
        "side_effects": False,
        "scenario": {"name": "MAP_SH_PRODUCTS_TO_ODS_SCN", "version": "001"},
        "checks": [
            {"name": "scenario_found", "status": "fail"},
            {"name": "forbidden_operations_not_called", "status": "pass"},
        ],
    }


def test_products_scenario_readiness_accepts_only_exact_reviewed_contract():
    from products_ods_scenario_readiness import build_products_ods_scenario_readiness

    result = build_products_ods_scenario_readiness(
        _mapping_inspection(),
        _absent_scenario_probe(),
        mapping_review_status="approved",
        approved=True,
        approval_scope="odi68_products_ods_scenario_generation",
    )

    assert result["status"] == "ready_for_absent_only_scenario_generation"
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False
    assert result["scenario"]["name"] == "MAP_SH_PRODUCTS_TO_ODS_SCN"


@pytest.mark.parametrize("approved", [False, None, "yes", 1])
def test_products_scenario_readiness_rejects_non_literal_approval(approved):
    from products_ods_scenario_readiness import build_products_ods_scenario_readiness

    result = build_products_ods_scenario_readiness(
        _mapping_inspection(),
        _absent_scenario_probe(),
        mapping_review_status="approved",
        approved=approved,
        approval_scope="odi68_products_ods_scenario_generation",
    )

    assert result["status"] == "blocked_until_explicit_approval"


@pytest.mark.parametrize(
    "physical,logical",
    [
        ({"target_ikm": "IKM Oracle Insert", "lkm": "LKM SQL to Oracle (Built-In)"}, None),
        ({"target_ikm": "IKM Oracle Merge", "lkm": "LKM Oracle to Oracle Pull (DB Link)"}, None),
        (None, {"integration_type": "INCREMENTAL_UPDATE", "update_key_columns": []}),
    ],
)
def test_products_scenario_readiness_fails_closed_on_mapping_drift(physical, logical):
    from products_ods_scenario_readiness import build_products_ods_scenario_readiness

    inspection = _mapping_inspection()
    if physical is not None:
        inspection["physical"] = physical
    if logical is not None:
        inspection["logical"] = logical

    result = build_products_ods_scenario_readiness(
        inspection,
        _absent_scenario_probe(),
        mapping_review_status="approved",
        approved=True,
        approval_scope="odi68_products_ods_scenario_generation",
    )

    assert result["status"] == "blocked_mapping_contract"


def test_products_scenario_readiness_blocks_when_exact_scenario_exists():
    from products_ods_scenario_readiness import build_products_ods_scenario_readiness

    probe = _absent_scenario_probe()
    probe["status"] = "passed"
    probe["checks"][0]["status"] = "pass"
    result = build_products_ods_scenario_readiness(
        _mapping_inspection(),
        probe,
        mapping_review_status="approved",
        approved=True,
        approval_scope="odi68_products_ods_scenario_generation",
    )

    assert result["status"] == "blocked_scenario_already_exists"


def test_products_scenario_readiness_has_no_connection_or_execution_surface():
    source = (PROJECT_ROOT / "products_ods_scenario_readiness.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for token in ("oracledb", "connect(", "run_groovy", "generate_scenario", "startscenario"):
        assert token not in lowered


def test_products_scenario_readiness_runbook_keeps_generation_and_run_separate():
    content = (PROJECT_ROOT / "docs" / "dwh-products-ods-scenario-readiness.md").read_text(
        encoding="utf-8"
    )

    assert "does not generate or\nrun a scenario" in content
    assert "Scenario execution remains a different approval boundary" in content
    assert "DB-Link LKM" in content
