from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mapping_inspection():
    return {
        "status": "passed",
        "side_effects": False,
        "physical": {
            "target_ikm": "IKM Oracle Merge",
            "lkm": "LKM SQL to Oracle (Built-In)",
        },
        "logical": {
            "integration_type": "INCREMENTAL_UPDATE",
            "update_key_columns": ["PROD_ID"],
            "audit_expressions": {
                "ODS_CREATED_AT": "CURRENT_TIMESTAMP",
                "ODS_UPDATED_AT": "CURRENT_TIMESTAMP",
            },
        },
    }


def _scenario_probe():
    return {
        "status": "passed",
        "side_effects": False,
        "scenario": {
            "name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
            "version": "001",
        },
        "checks": [
            {"name": "scenario_found", "status": "pass"},
            {"name": "forbidden_operations_not_called", "status": "pass"},
        ],
    }


def _candidate_preflight():
    return {
        "status": "ready_for_type1_backup",
        "source_count": 72,
        "target_count": 0,
        "source_duplicate_key_group_count": 0,
        "target_duplicate_key_group_count": 0,
        "insert_candidate_count": 72,
        "update_candidate_count": 0,
        "unchanged_count": 0,
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def _backup_proof():
    return {
        "status": "passed",
        "backup_exists": True,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "restore_proof_exists_after_cleanup": False,
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def _build(**overrides):
    from products_ods_scenario_run_readiness import (
        SCENARIO_RUN_APPROVAL_SCOPE,
        build_products_ods_scenario_run_readiness,
    )

    values = {
        "mapping_inspection": _mapping_inspection(),
        "scenario_probe": _scenario_probe(),
        "candidate_preflight": _candidate_preflight(),
        "backup_proof": _backup_proof(),
        "scenario_generation_review_status": "approved",
        "approved": True,
        "approval_scope": SCENARIO_RUN_APPROVAL_SCOPE,
    }
    values.update(overrides)
    return build_products_ods_scenario_run_readiness(**values)


def test_run_readiness_accepts_only_the_exact_reviewed_products_contract():
    result = _build()

    assert result["status"] == "ready_for_separate_scenario_run_executor"
    assert result["scenario"] == {
        "name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
        "version": "001",
        "context": "GLOBAL",
    }
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False


def test_run_readiness_requires_generation_review():
    result = _build(scenario_generation_review_status="pending")

    assert result["status"] == "blocked_scenario_generation_review"


def test_run_readiness_rejects_mapping_drift():
    mapping = _mapping_inspection()
    mapping["physical"]["lkm"] = "LKM Oracle to Oracle Pull (DB Link)"

    assert _build(mapping_inspection=mapping)["status"] == "blocked_mapping_contract"


@pytest.mark.parametrize(
    "probe",
    [
        {**_scenario_probe(), "status": "attention_required"},
        {
            **_scenario_probe(),
            "scenario": {"name": "MAP_SH_PRODUCTS_TO_ODS_SCN", "version": "002"},
        },
        {
            **_scenario_probe(),
            "checks": [{"name": "scenario_found", "status": "fail"}],
        },
    ],
)
def test_run_readiness_requires_exact_read_only_scenario_proof(probe):
    assert _build(scenario_probe=probe)["status"] == "blocked_scenario_probe"


def test_run_readiness_rejects_inconsistent_candidate_counts():
    candidate = _candidate_preflight()
    candidate["insert_candidate_count"] = 71

    assert _build(candidate_preflight=candidate)["status"] == "blocked_candidate_preflight"


def test_run_readiness_requires_matching_named_backup_proof():
    backup = _backup_proof()
    backup["backup_row_count"] = 1

    assert _build(backup_proof=backup)["status"] == "blocked_named_backup_proof"


@pytest.mark.parametrize("approved", [False, "yes", 1, None])
def test_run_readiness_rejects_non_literal_approval(approved):
    assert _build(approved=approved)["status"] == "blocked_until_exact_operator_approval"


def test_run_readiness_rejects_wrong_scope():
    assert _build(approval_scope="odi68_products_ods_scenario_generation")["status"] == "blocked_approval_scope"


def test_run_readiness_has_no_connection_or_execution_surface():
    text = (PROJECT_ROOT / "products_ods_scenario_run_readiness.py").read_text(encoding="utf-8")
    lowered = text.lower()

    assert "oracledb" not in lowered
    assert "odi_orchestrator" not in lowered
    assert "subprocess" not in lowered
    assert '"will_execute": false' in lowered
    assert '"connection_attempted": false' in lowered


def test_run_readiness_runbook_keeps_live_run_separately_reviewed():
    text = (PROJECT_ROOT / "docs" / "dwh-products-ods-scenario-run-readiness.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()

    for required in (
        "named backup",
        "exact operator approval",
        "separate run executor",
        "oracle/odi commits",
        "no automatic rollback",
        "no scenario run was performed",
        "no raw rows",
        "no pk values",
    ):
        assert required in lowered
