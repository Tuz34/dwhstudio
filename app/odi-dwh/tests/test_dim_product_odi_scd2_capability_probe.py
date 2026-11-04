import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT_NAME = "dim_product_scd2_capability_probe.groovy"


def _tracked_attributes() -> list[str]:
    from dim_product_odi_scd2_packaging_readiness import EXPECTED_SCD_ROLES

    return list(EXPECTED_SCD_ROLES["tracked_attributes"])


def _raw_probe_payload(**overrides):
    payload = {
        "tool": "odi.dim_product_scd2_capability_probe",
        "status": "passed",
        "mode": "odi_sdk_finders_reflection_only",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "capability": {
            "status": "passed",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "target_technology": "ORACLE",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "target_ikm_installed": True,
            "target_ikm_assignable": True,
            "lkm": "LKM SQL to Oracle (Built-In)",
            "scd2_role_assignment_supported": True,
        },
        "datastore": {
            "status": "passed",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "source": {
                "owner": "DWH",
                "table": "ODS_SH_PRODUCTS",
                "column_count": 24,
            },
            "target": {
                "owner": "DWH",
                "table": "DIM_PRODUCT",
                "column_count": 26,
            },
            "target_keys": {
                "primary_key_columns": ["PRODUCT_SK"],
                "natural_key_columns": ["PROD_ID"],
            },
            "scd_roles": {
                "surrogate_key": ["PRODUCT_SK"],
                "natural_key": ["PROD_ID"],
                "start_date": ["EFFECTIVE_FROM"],
                "end_date": ["EFFECTIVE_TO"],
                "current_flag": ["CURRENT_FLAG"],
                "tracked_attributes": _tracked_attributes(),
            },
        },
        "objects": {
            "status": "attention_required",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "mapping": {
                "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
                "exists": False,
            },
            "scenario": {
                "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
                "version": "001",
                "exists": False,
            },
        },
    }
    payload.update(overrides)
    return payload


def test_normalizer_accepts_only_the_fixed_metadata_shape():
    from dim_product_odi_scd2_capability_probe import (
        normalize_dim_product_odi_scd2_capability_probe,
    )

    result = normalize_dim_product_odi_scd2_capability_probe(_raw_probe_payload())

    assert result["status"] == "passed"
    assert result["side_effects"] is False
    assert result["repository_connection_attempted"] is True
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["secret_printed"] is False
    assert result["readiness"]["status"] == (
        "ready_for_separate_mapping_builder_implementation_review"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": "not allowed"}),
        lambda value: value["capability"].update({"option_value": "not allowed"}),
        lambda value: value["datastore"]["source"].update(
            {"sample_key": "not allowed"}
        ),
        lambda value: value["objects"]["mapping"].update(
            {"repository_object_id": "not allowed"}
        ),
        lambda value: value["objects"]["mapping"].update({"exists": 1}),
        lambda value: value.update({"raw_rows_included": True}),
    ],
)
def test_normalizer_rejects_extra_or_unsafe_payload_without_echoing_it(mutate):
    from dim_product_odi_scd2_capability_probe import (
        normalize_dim_product_odi_scd2_capability_probe,
    )

    payload = _raw_probe_payload()
    mutate(payload)
    result = normalize_dim_product_odi_scd2_capability_probe(payload)

    assert result["status"] == "blocked_invalid_probe_output"
    assert result["error"] == "ODI capability probe output was rejected."
    assert "unexpected" not in json.dumps(result).lower()
    assert "sample_key" not in json.dumps(result).lower()
    assert "repository_object_id" not in json.dumps(result).lower()


def test_normalizer_returns_class_only_error_for_fixed_error_payload():
    from dim_product_odi_scd2_capability_probe import (
        normalize_dim_product_odi_scd2_capability_probe,
    )

    result = normalize_dim_product_odi_scd2_capability_probe(
        {
            "tool": "odi.dim_product_scd2_capability_probe",
            "status": "error",
            "mode": "odi_sdk_finders_reflection_only",
            "side_effects": False,
            "repository_connection_attempted": True,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "error_type": "IllegalStateException",
        }
    )

    assert result == {
        "tool": "odi.dim_product_scd2_capability_probe",
        "status": "error",
        "mode": "normalized_odi_sdk_metadata_only",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "error": "ODI capability probe failed.",
        "error_type": "IllegalStateException",
    }


@pytest.mark.parametrize("approval", [False, None, 1, "yes", "true"])
def test_orchestrator_wrapper_requires_literal_true_before_groovy(approval, monkeypatch):
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "run_groovy",
        lambda script, params: calls.append((script, params)) or "should not run",
    )

    result = orchestrator.dim_product_scd2_capability_probe(approved=approval)

    assert result["status"] == "blocked_until_approval"
    assert result["approval"]["action"] == "ODI_REPOSITORY_READ_ONLY"
    assert result["repository_connection_attempted"] is False
    assert calls == []


def test_orchestrator_wrapper_uses_fixed_script_and_only_project_folder(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []

    def fake_run(script, params):
        calls.append((script, params))
        return json.dumps(_raw_probe_payload())

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)

    result = orchestrator.dim_product_scd2_capability_probe(approved=True)

    assert result["status"] == "passed"
    assert calls == [
        (
            SCRIPT_NAME,
            {
                "odi.project": odi_orchestrator.ODI_PROJECT,
                "odi.folder": odi_orchestrator.ODI_FOLDER,
            },
        )
    ]


def test_orchestrator_wrapper_rejects_non_json_without_returning_raw_body(monkeypatch):
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "run_groovy",
        lambda _script, _params: "driver detail must not be returned",
    )

    result = orchestrator.dim_product_scd2_capability_probe(approved=True)

    assert result["status"] == "error"
    assert result["error"] == "ODI capability probe output parsing failed."
    assert result["error_type"] == "JSONDecodeError"
    assert "driver detail" not in json.dumps(result)


def test_groovy_probe_is_registered_read_only_and_uses_fixed_identities():
    import odi_orchestrator

    script = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(
        encoding="utf-8"
    )
    lower = script.lower()

    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[SCRIPT_NAME].category == "read_only"
    for required in (
        "TGT_DWH",
        "ODS_SH_PRODUCTS",
        "DIM_PRODUCT",
        "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
        "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
        "IKM Oracle Slowly Changing Dimension",
        "LKM SQL to Oracle (Built-In)",
        "getScdType",
        "SURROGATE_KEY",
        "NATURAL_KEY",
        "ADD_ROW_ON_CHANGE",
        "CURRENT_RECORD_FLAG",
        "START_TIMESTAMP",
        "END_TIMESTAMP",
    ):
        assert required in script

    for forbidden in (
        ".persist(",
        ".remove(",
        "commit(",
        "generatescenario",
        "startscenario",
        "setscdtype",
        "getinternalid",
        "getexpression",
        "getoptions",
        "error_detail",
        "stacktrace",
        "insert into",
        "update ",
        "delete from",
        "create table",
        "drop table",
        "truncate",
    ):
        assert forbidden not in lower

    assert "odi.src_model" not in script
    assert "odi.tgt_model" not in script
    assert "odi.mapping" not in script
    assert "odi.scenario.name" not in script
