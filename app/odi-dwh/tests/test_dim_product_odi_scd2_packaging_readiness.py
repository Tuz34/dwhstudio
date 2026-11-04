from pathlib import Path

import pytest

from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan


PROJECT_ROOT = Path(__file__).parents[1]


def _tracked_attributes() -> list[str]:
    metadata = [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
    ]
    return list(build_dim_product_scd2_plan(metadata)["tracked_attributes"])


def _capability_report(**overrides):
    report = {
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
    }
    report.update(overrides)
    return report


def _datastore_report(**overrides):
    report = {
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
    }
    report.update(overrides)
    return report


def _object_probe(**overrides):
    report = {
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
    }
    report.update(overrides)
    return report


def _evaluate(*, capability=None, datastore=None, object_probe=None):
    from dim_product_odi_scd2_packaging_readiness import (
        build_dim_product_odi_scd2_packaging_readiness,
    )

    return build_dim_product_odi_scd2_packaging_readiness(
        capability or _capability_report(),
        datastore or _datastore_report(),
        object_probe or _object_probe(),
    )


def test_static_packaging_plan_rejects_generic_type1_builder_reuse():
    from dim_product_odi_scd2_packaging_readiness import (
        build_dim_product_odi_scd2_packaging_plan,
    )

    plan = build_dim_product_odi_scd2_packaging_plan()

    assert plan["status"] == "blocked_pending_read_only_odi_capability_probe"
    assert plan["source"] == {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
    assert plan["target"] == {"owner": "DWH", "table": "DIM_PRODUCT"}
    assert plan["mapping"] == "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
    assert plan["scenario"] == {
        "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
        "version": "001",
    }
    assert plan["required_target_ikm"] == "IKM Oracle Slowly Changing Dimension"
    assert plan["existing_generic_builder_reusable"] is False
    assert plan["server_side_executor_remains_baseline"] is True
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False


def test_ready_contract_freezes_exact_scd2_roles_and_stays_non_executable():
    result = _evaluate()

    assert result["status"] == "ready_for_separate_mapping_builder_implementation_review"
    assert result["mapping_collision_free"] is True
    assert result["scenario_collision_free"] is True
    assert result["target_ikm_verified"] is True
    assert result["scd2_roles_verified"] is True
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False
    assert result["write_operations_attempted"] == []
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["secret_printed"] is False
    assert result["required_next"] == [
        "implement_dedicated_absent_only_scd2_mapping_builder",
        "independently_review_mapping_builder",
        "request_exact_mapping_creation_approval",
        "inspect_created_mapping_read_only",
        "implement_and_review_scenario_generation_separately",
        "request_separate_scenario_run_approval",
    ]


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ({"status": "unavailable"}, "blocked_capability_probe"),
        ({"side_effects": True}, "blocked_unsafe_capability_evidence"),
        ({"raw_rows_included": True}, "blocked_unsafe_capability_evidence"),
        ({"unexpected_payload": "not_allowed"}, "blocked_unsafe_capability_evidence"),
        ({"target_technology": "GENERIC_SQL"}, "blocked_target_technology"),
        ({"target_ikm_installed": False}, "blocked_scd2_ikm_missing"),
        ({"target_ikm_assignable": False}, "blocked_scd2_ikm_not_assignable"),
        ({"target_ikm": "IKM Oracle Merge"}, "blocked_scd2_ikm_contract"),
        ({"lkm": "LKM Oracle to Oracle Pull (DB Link)"}, "blocked_lkm_contract"),
        ({"scd2_role_assignment_supported": False}, "blocked_scd2_role_api"),
    ],
)
def test_capability_evidence_fails_closed(capability, expected):
    result = _evaluate(capability=_capability_report(**capability))

    assert result["status"] == expected
    assert result["will_execute"] is False


@pytest.mark.parametrize(
    ("datastore", "expected"),
    [
        ({"status": "unavailable"}, "blocked_datastore_probe"),
        ({"side_effects": True}, "blocked_unsafe_datastore_evidence"),
        ({"raw_rows_included": True}, "blocked_unsafe_datastore_evidence"),
        (
            {"source": {"owner": "SH", "table": "PRODUCTS", "column_count": 22}},
            "blocked_source_datastore_contract",
        ),
        (
            {
                "source": {
                    "owner": "DWH",
                    "table": "ODS_SH_PRODUCTS",
                    "column_count": 24,
                    "sample_key": "not_allowed",
                }
            },
            "blocked_source_datastore_contract",
        ),
        (
            {"target": {"owner": "DWH", "table": "DIM_PRODUCT", "column_count": 25}},
            "blocked_target_datastore_contract",
        ),
        (
            {"target_keys": {"primary_key_columns": [], "natural_key_columns": ["PROD_ID"]}},
            "blocked_target_key_contract",
        ),
        (
            {"scd_roles": {"surrogate_key": ["PRODUCT_SK"]}},
            "blocked_scd2_role_contract",
        ),
    ],
)
def test_datastore_and_role_evidence_fail_closed(datastore, expected):
    result = _evaluate(datastore=_datastore_report(**datastore))

    assert result["status"] == expected
    assert result["will_execute"] is False


@pytest.mark.parametrize(
    ("object_probe", "expected"),
    [
        ({"status": "unavailable"}, "blocked_object_probe"),
        ({"side_effects": True}, "blocked_unsafe_object_evidence"),
        ({"raw_rows_included": True}, "blocked_unsafe_object_evidence"),
        (
            {"mapping": {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT", "exists": True}},
            "blocked_mapping_already_exists",
        ),
        (
            {
                "scenario": {
                    "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
                    "version": "001",
                    "exists": True,
                }
            },
            "blocked_scenario_already_exists",
        ),
        (
            {"mapping": {"name": "MAP_OTHER", "exists": False}},
            "blocked_object_identity_contract",
        ),
        (
            {
                "mapping": {
                    "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
                    "exists": False,
                    "repository_object_id": "not_allowed",
                }
            },
            "blocked_object_identity_contract",
        ),
    ],
)
def test_mapping_and_scenario_collision_evidence_fail_closed(object_probe, expected):
    result = _evaluate(object_probe=_object_probe(**object_probe))

    assert result["status"] == expected
    assert result["will_execute"] is False


def test_existing_generic_mapping_builder_is_type1_only_for_this_contract():
    content = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(
        encoding="utf-8"
    )

    assert "IKM Oracle Merge" in content
    assert "Required update key is missing or mismatched" in content
    assert "IKM Oracle Slowly Changing Dimension" not in content


def test_packaging_readiness_module_has_no_runtime_or_write_surface():
    source = (
        PROJECT_ROOT / "dim_product_odi_scd2_packaging_readiness.py"
    ).read_text(encoding="utf-8").lower()

    for forbidden in (
        "oracledb",
        "connect(",
        "run_groovy",
        "subprocess",
        "from config",
        "import config",
        "@app.route",
        "blueprint",
        "insert into",
        "update ",
        "delete from",
        "create mapping",
        "generate scenario",
        "startscenario",
    ):
        assert forbidden not in source
