from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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


def _candidate(*, observed_at="2026-01-01T00:00:01+00:00"):
    return {
        "status": "rerun_noop",
        "observed_at": observed_at,
        "connection_attempted": True,
        "write_attempted": False,
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
        executor_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert result["status"] == "ready_for_separate_scenario_run_executor"
    assert result["will_execute"] is False
    assert result["requires_two_noop_runs"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_count", 71),
        ("target_count", 71),
        ("current_target_count", 71),
        ("unchanged_count", 71),
        ("source_duplicate_key_group_count", 1),
        ("target_duplicate_surrogate_key_group_count", 1),
        ("target_multiple_current_key_group_count", 1),
        ("target_invalid_current_flag_count", 1),
        ("target_invalid_effective_range_count", 1),
        ("source_missing_current_count", 1),
        ("insert_candidate_count", 1),
        ("changed_candidate_count", 1),
        ("candidate_count", 1),
        ("backup_exists", 0),
        ("backup_column_count", 25),
        ("backup_row_count", 1),
        ("connection_attempted", False),
        ("write_attempted", True),
        ("raw_rows_included", True),
        ("pk_values_included", True),
        ("secret_printed", True),
    ],
)
def test_run_readiness_rejects_count_or_safety_mismatch(field, value):
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    candidate = _candidate()
    candidate[field] = value
    result = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(),
        _scenario_probe(exists=True),
        candidate,
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope=RUN_SCOPE,
        executor_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_candidate_preflight"
    assert result["connection_attempted"] is False


@pytest.mark.parametrize(
    ("started_at", "observed_at"),
    [
        (None, "2026-01-01T00:00:01+00:00"),
        (datetime(2026, 1, 1), "2026-01-01T00:00:01+00:00"),
        (datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))), "2026-01-01T00:00:01+00:00"),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), None),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), "2026-01-01T00:00:01"),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), "bad timestamp"),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), "2025-12-31T23:59:59+00:00"),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), "2026-01-01T01:00:01+01:00"),
    ],
)
def test_run_readiness_rejects_missing_invalid_or_stale_evidence(started_at, observed_at):
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    result = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(),
        _scenario_probe(exists=True),
        _candidate(observed_at=observed_at),
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope=RUN_SCOPE,
        executor_started_at=started_at,
    )

    assert result["status"] == "blocked_candidate_preflight_freshness"
    assert result["connection_attempted"] is False


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "source_count",
        "target_count",
        "current_target_count",
        "source_duplicate_key_group_count",
        "target_duplicate_surrogate_key_group_count",
        "target_multiple_current_key_group_count",
        "target_invalid_current_flag_count",
        "target_invalid_effective_range_count",
        "source_missing_current_count",
        "insert_candidate_count",
        "changed_candidate_count",
        "unchanged_count",
        "candidate_count",
        "backup_exists",
        "backup_column_count",
        "backup_row_count",
        "connection_attempted",
        "write_attempted",
        "raw_rows_included",
        "pk_values_included",
        "secret_printed",
    ],
)
def test_run_readiness_rejects_missing_preflight_field(field):
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    candidate = _candidate()
    candidate.pop(field)
    result = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(),
        _scenario_probe(exists=True),
        candidate,
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope=RUN_SCOPE,
        executor_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert result["status"] == "blocked_candidate_preflight"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "OTHER_MAPPING"),
        ("source_component_count", True),
        ("target_component_count", True),
        ("target_attribute_count", 25),
        ("mapped_attribute_count", 25),
        ("integration_type", "IU"),
        ("target_ikm", "OTHER_IKM"),
        ("access_point_count", -1),
        ("lkm_assigned_count", 1),
    ],
)
def test_run_readiness_rejects_invalid_mapping_metadata(field, value):
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    probe = _mapping_probe()
    probe["mapping"][field] = value
    result = build_dim_product_scd2_scenario_run_readiness(
        probe,
        _scenario_probe(exists=True),
        _candidate(),
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope=RUN_SCOPE,
        executor_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert result["status"] == "blocked_mapping_probe"


@pytest.mark.parametrize("approved", [False, None, "yes", 1])
def test_run_readiness_rejects_nonliteral_approval(approved):
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    result = build_dim_product_scd2_scenario_run_readiness(
        _mapping_probe(),
        _scenario_probe(exists=True),
        _candidate(),
        scenario_generation_review_status="approved",
        approved=approved,
        approval_scope=RUN_SCOPE,
        executor_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert result["status"] == "blocked_until_exact_run_approval"


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

