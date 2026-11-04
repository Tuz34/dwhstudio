from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATION_SCOPE = "odi68_dim_product_odi_scd2_scenario_generation"
RUN_SCOPE = "odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof"


def _mapping_probe():
    return {
        "tool": "odi.dim_product_scd2_mapping_probe",
        "status": "passed",
        "mode": "normalized_fixed_scd2_mapping_metadata",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "mapping": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
            "source_component_count": 1,
            "target_component_count": 1,
            "target_attribute_count": 26,
            "mapped_attribute_count": 26,
            "integration_type": "SCD",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "access_point_count": 0,
            "lkm_assigned_count": 0,
        },
    }


def _scenario_probe(*, exists: bool):
    return {
        "status": "passed" if exists else "attention_required",
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "scenario": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
            "version": "001",
            "exists": exists,
        },
    }


def _candidate():
    return {
        "status": "rerun_noop",
        "source_count": 72,
        "target_count": 72,
        "current_target_count": 72,
        "source_duplicate_key_group_count": 0,
        "target_duplicate_surrogate_key_group_count": 0,
        "target_multiple_current_key_group_count": 0,
        "target_invalid_current_flag_count": 0,
        "target_invalid_effective_range_count": 0,
        "source_missing_current_count": 0,
        "insert_candidate_count": 0,
        "changed_candidate_count": 0,
        "unchanged_count": 72,
        "candidate_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _raw_generation_success():
    return {
        "tool": "odi.generate_scenario_metadata",
        "status": "passed",
        "mode": "scenario_metadata_generation",
        "side_effects": True,
        "repository_connection_attempted": True,
        "scenario": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
            "version": "001",
        },
        "checks": [
            {"name": "scenario_generated", "status": "pass"},
            {"name": "forbidden_operations_not_called", "status": "pass"},
        ],
    }


def test_scenario_generation_readiness_requires_exact_mapping_and_absent_scenario():
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_generation_readiness

    result = build_dim_product_scd2_scenario_generation_readiness(
        _mapping_probe(),
        _scenario_probe(exists=False),
        mapping_builder_review_status="approved",
        approved=True,
        approval_scope=GENERATION_SCOPE,
    )

    assert result["status"] == "ready_for_absent_only_scenario_generation"
    assert result["will_execute"] is False
    assert result["scenario_run_included"] is False


@pytest.mark.parametrize("approved", [False, None, "yes", 1])
def test_scenario_generation_readiness_rejects_non_literal_approval(approved):
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_generation_readiness

    result = build_dim_product_scd2_scenario_generation_readiness(
        _mapping_probe(),
        _scenario_probe(exists=False),
        mapping_builder_review_status="approved",
        approved=approved,
        approval_scope=GENERATION_SCOPE,
    )
    assert result["status"] == "blocked_until_exact_generation_approval"


def test_generation_normalizer_strips_checks_and_accepts_only_fixed_identity():
    from dim_product_odi_scd2_scenario import normalize_dim_product_scd2_scenario_generation

    result = normalize_dim_product_scd2_scenario_generation(_raw_generation_success())

    assert result["status"] == "passed"
    assert "checks" not in result
    assert result["scenario"] == {
        "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
        "version": "001",
    }


def test_generation_normalizer_rejects_extra_scenario_metadata():
    from dim_product_odi_scd2_scenario import normalize_dim_product_scd2_scenario_generation

    payload = deepcopy(_raw_generation_success())
    payload["scenario"]["internal_id"] = "not-allowed"
    result = normalize_dim_product_scd2_scenario_generation(payload)

    assert result["status"] == "blocked_invalid_generation_output"
    assert "not-allowed" not in json.dumps(result)


def test_run_readiness_requires_exact_generated_scenario_and_clean_noop_preflight():
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    result = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(),
        _scenario_probe(exists=True),
        _candidate(),
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope=RUN_SCOPE,
    )

    assert result["status"] == "ready_for_separate_scenario_run_executor"
    assert result["will_execute"] is False
    assert result["requires_two_noop_runs"] is True


def test_run_readiness_blocks_candidates_or_missing_backup():
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    candidate = _candidate()
    candidate["changed_candidate_count"] = 1
    candidate["candidate_count"] = 1
    result = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(),
        _scenario_probe(exists=True),
        candidate,
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope=RUN_SCOPE,
    )
    assert result["status"] == "blocked_candidate_preflight"


def test_scenario_orchestrator_generation_uses_fixed_identity(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []

    def fake_run(script, params):
        calls.append((script, params, odi_orchestrator._GROOVY_APPROVED.get()))
        return json.dumps(_raw_generation_success())

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)
    result = orchestrator.dim_product_scd2_scenario_generate(
        mapping_probe=_mapping_probe(),
        scenario_probe=_scenario_probe(exists=False),
        mapping_builder_review_status="approved",
        scenario_generator_review_status="approved",
        approved=True,
        approval_scope=GENERATION_SCOPE,
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            "generate_scenario_metadata.groovy",
            {
                "odi.project": odi_orchestrator.ODI_PROJECT,
                "odi.folder": odi_orchestrator.ODI_FOLDER,
                "odi.mapping": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
                "odi.scenario.name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
                "odi.scenario.version": "001",
            },
            True,
        )
    ]


def test_scenario_package_has_no_run_or_database_connection_surface():
    source = (PROJECT_ROOT / "dim_product_odi_scd2_scenario.py").read_text(encoding="utf-8")
    lowered = source.lower()

    for forbidden in (
        "oracledb",
        "connect(",
        "startscenario",
        "run_scenario",
        "scenario_runner",
        "insert into",
        "update dwh.",
        "delete from",
    ):
        assert forbidden not in lowered

