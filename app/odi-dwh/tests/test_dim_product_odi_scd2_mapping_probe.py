from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAME = "dim_product_scd2_mapping_probe.groovy"


def _raw():
    return {
        "tool": "odi.dim_product_scd2_mapping_probe",
        "status": "passed",
        "mode": "fixed_scd2_mapping_metadata_probe",
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


def test_mapping_probe_normalizer_accepts_only_exact_count_status_metadata():
    from dim_product_odi_scd2_mapping_probe import (
        normalize_dim_product_odi_scd2_mapping_probe,
    )

    result = normalize_dim_product_odi_scd2_mapping_probe(_raw())

    assert result["status"] == "passed"
    assert result["mapping"]["mapped_attribute_count"] == 26
    assert result["raw_rows_included"] is False


def test_mapping_probe_normalizer_rejects_expression_or_repository_metadata():
    from dim_product_odi_scd2_mapping_probe import (
        normalize_dim_product_odi_scd2_mapping_probe,
    )

    payload = deepcopy(_raw())
    payload["mapping"]["expression"] = "not-allowed"
    result = normalize_dim_product_odi_scd2_mapping_probe(payload)

    assert result["status"] == "blocked_invalid_mapping_probe_output"
    assert "not-allowed" not in json.dumps(result)


@pytest.mark.parametrize("name", ["source_component_count", "target_component_count", "target_attribute_count", "mapped_attribute_count"])
@pytest.mark.parametrize("bad", [True, False, "1", 1.0, 26.0, None])
def test_mapping_probe_requires_integer_component_counts(name, bad):
    from dim_product_odi_scd2_mapping_probe import normalize_dim_product_odi_scd2_mapping_probe

    payload = _raw()
    payload["mapping"][name] = bad
    assert normalize_dim_product_odi_scd2_mapping_probe(payload)["status"] == "blocked_invalid_mapping_probe_output"


def test_mapping_probe_script_is_finder_only_and_registered_read_only():
    import odi_orchestrator

    source = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(encoding="utf-8")
    lowered = source.lower()

    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[SCRIPT_NAME].category == "read_only"
    for forbidden in (
        ".persist(",
        ".remove(",
        ".commit(",
        "generate",
        "startscenario",
        "getinternalid",
        "getexpressiontext",
        "printstacktrace",
    ):
        assert forbidden not in lowered
    assert '"SLOWLY_CHANGING_DIMENSION"' in source
    assert 'integration_type: "SCD"' in source


def test_mapping_probe_wrapper_requires_literal_read_only_approval(monkeypatch):
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "run_groovy",
        lambda script, params: calls.append((script, params)) or json.dumps(_raw()),
    )

    assert orchestrator.dim_product_scd2_mapping_probe(approved="yes")["status"] == "blocked_until_approval"
    assert calls == []
    assert orchestrator.dim_product_scd2_mapping_probe(approved=True)["status"] == "passed"
    assert calls[0][0] == SCRIPT_NAME
