from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAME = "dim_product_scd2_ikm_import.groovy"
APPROVAL_SCOPE = "odi68_dim_product_scd2_ikm_import"


def _probe(*, installed: bool = False, assignable: bool = False):
    return {
        "tool": "odi.dim_product_scd2_capability_probe",
        "status": "passed",
        "mode": "normalized_odi_sdk_metadata_only",
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
            "target_ikm": "IKM Oracle Slowly Changing Dimension" if installed else None,
            "target_ikm_installed": installed,
            "target_ikm_assignable": assignable,
            "lkm": "LKM SQL to Oracle (Built-In)",
            "scd2_role_assignment_supported": True,
        },
        "datastore": {
            "status": "passed",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "source": {"owner": "DWH", "table": "ODS_SH_PRODUCTS", "column_count": 24},
            "target": {"owner": "DWH", "table": "DIM_PRODUCT", "column_count": 26},
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
                "tracked_attributes": [
                    "PROD_NAME",
                    "PROD_DESC",
                    "PROD_SUBCATEGORY",
                    "PROD_SUBCATEGORY_ID",
                    "PROD_SUBCATEGORY_DESC",
                    "PROD_CATEGORY",
                    "PROD_CATEGORY_ID",
                    "PROD_CATEGORY_DESC",
                    "PROD_WEIGHT_CLASS",
                    "PROD_UNIT_OF_MEASURE",
                    "PROD_PACK_SIZE",
                    "SUPPLIER_ID",
                    "PROD_STATUS",
                    "PROD_LIST_PRICE",
                    "PROD_MIN_PRICE",
                    "PROD_TOTAL",
                    "PROD_TOTAL_ID",
                ],
            },
        },
        "objects": {
            "status": "attention_required",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "mapping": {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT", "exists": False},
            "scenario": {
                "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
                "version": "001",
                "exists": False,
            },
        },
    }


def _request(**overrides):
    from dim_product_odi_scd2_ikm_import import (
        build_dim_product_odi_scd2_ikm_import_plan,
        build_dim_product_odi_scd2_ikm_import_request,
    )

    values = {
        "plan": build_dim_product_odi_scd2_ikm_import_plan(_probe()),
        "capability_probe_review_status": "approved",
        "import_contract_review_status": "approved",
        "importer_implementation_review_status": "approved",
        "approved": True,
        "approval_scope": APPROVAL_SCOPE,
    }
    values.update(overrides)
    return build_dim_product_odi_scd2_ikm_import_request(**values)


def _raw_success():
    return {
        "tool": "odi.dim_product_scd2_ikm_import",
        "status": "passed",
        "mode": "fixed_sdk_reference_absent_only_ikm_import",
        "side_effects": True,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "result": {
            "status": "passed",
            "ikm_name": "IKM Oracle Slowly Changing Dimension",
            "target_technology": "ORACLE",
            "integration_type": "SLOWLY_CHANGING_DIMENSION",
            "import_mode": "DUPLICATION",
            "existing_before": False,
            "import_attempted": True,
            "post_import_verified": True,
        },
    }


def test_import_plan_accepts_only_registered_target_with_missing_fixed_ikm():
    from dim_product_odi_scd2_ikm_import import build_dim_product_odi_scd2_ikm_import_plan

    plan = build_dim_product_odi_scd2_ikm_import_plan(_probe())

    assert plan["status"] == "ready_for_importer_implementation_review"
    assert plan["reference_file_name"] == "KM_IKM Oracle Slowly Changing Dimension.xml"
    assert plan["import_mode"] == "DUPLICATION"
    assert plan["absent_only"] is True
    assert plan["replace_existing_ikm"] is False
    assert plan["will_execute"] is False


def test_import_plan_blocks_when_fixed_ikm_already_exists():
    from dim_product_odi_scd2_ikm_import import build_dim_product_odi_scd2_ikm_import_plan

    assert (
        build_dim_product_odi_scd2_ikm_import_plan(
            _probe(installed=True, assignable=True)
        )["status"]
        == "blocked_ikm_already_exists"
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"capability_probe_review_status": "pending"}, "blocked_capability_probe_review"),
        ({"import_contract_review_status": "pending"}, "blocked_import_contract_review"),
        ({"importer_implementation_review_status": "pending"}, "blocked_importer_implementation_review"),
        ({"approved": "yes"}, "blocked_until_explicit_approval"),
        ({"approved": 1}, "blocked_until_explicit_approval"),
        ({"approval_scope": "wrong"}, "blocked_until_exact_approval_scope"),
    ],
)
def test_import_request_fails_closed_before_repository_connection(overrides, expected):
    result = _request(**overrides)

    assert result["status"] == expected
    assert result["connection_attempted"] is False
    assert result["write_operations_attempted"] == []


def test_import_normalizer_accepts_only_exact_metadata():
    from dim_product_odi_scd2_ikm_import import normalize_dim_product_odi_scd2_ikm_import

    result = normalize_dim_product_odi_scd2_ikm_import(_raw_success())

    assert result["status"] == "passed"
    assert result["result"]["post_import_verified"] is True
    assert result["raw_rows_included"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.update({"unexpected": "repository-id"}),
        lambda p: p["result"].update({"file_path": "not-allowed"}),
        lambda p: p["result"].update({"post_import_verified": 1}),
    ],
)
def test_import_normalizer_rejects_extra_or_weak_fields(mutator):
    from dim_product_odi_scd2_ikm_import import normalize_dim_product_odi_scd2_ikm_import

    payload = deepcopy(_raw_success())
    mutator(payload)
    result = normalize_dim_product_odi_scd2_ikm_import(payload)

    assert result["status"] == "blocked_invalid_import_output"
    assert "repository-id" not in json.dumps(result)
    assert "not-allowed" not in json.dumps(result)


def test_fixed_importer_uses_official_duplication_api_and_has_no_replace_surface():
    import odi_orchestrator

    script = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(encoding="utf-8")
    lowered = script.lower()

    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[SCRIPT_NAME].category == "repository_write"
    for required in (
        "ImportServiceImpl",
        "IMPORT_MODE_DUPLICATION",
        'final String IKM_NAME = "IKM Oracle Slowly Changing Dimension"',
        'final String REFERENCE_FILE_NAME = "KM_IKM Oracle Slowly Changing Dimension.xml"',
        "target IKM already exists",
    ):
        assert required in script
    for forbidden in (
        "synonym_update",
        "synonym_insert_update",
        "importreplace",
        ".remove(",
        "delete ",
        "drop ",
        "printstacktrace",
        "globalid",
    ):
        assert forbidden not in lowered


def test_fixed_importer_declares_missing_origin_repository_for_sdk_reference():
    script = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(encoding="utf-8")

    assert """importService.importObjectFromXml(
        IImportService.IMPORT_MODE_DUPLICATION,
        referenceFile.getCanonicalPath(),
        project,
        true
    )""" in script
    assert "new DefaultTransactionDefinition()" in script
    assert (
        "import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition"
        in script
    )
    assert "org.springframework.transaction.support.DefaultTransactionDefinition" not in script
    assert "txnMgr.commit(txnStatus)" in script
    assert "txnMgr.rollback(txnStatus)" in script
    assert "IOdiProjectFinder" in script


def test_orchestrator_import_wrapper_uses_only_internal_oracle_home(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []

    def fake_run(script, params):
        calls.append((script, params, odi_orchestrator._GROOVY_APPROVED.get()))
        return json.dumps(_raw_success())

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)
    result = orchestrator.dim_product_scd2_ikm_import(
        capability_probe=_probe(),
        capability_probe_review_status="approved",
        import_contract_review_status="approved",
        importer_implementation_review_status="approved",
        approved=True,
        approval_scope=APPROVAL_SCOPE,
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            SCRIPT_NAME,
            {
                "odi.oracle_home": orchestrator.odi_home,
                "odi.project": odi_orchestrator.ODI_PROJECT,
            },
            True,
        )
    ]
    assert odi_orchestrator._GROOVY_APPROVED.get() is False
