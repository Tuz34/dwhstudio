"""Connectionless ODI SCD2 packaging readiness contract for ODI-68."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan


SOURCE = {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
TARGET = {"owner": "DWH", "table": "DIM_PRODUCT"}
MAPPING_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
SCENARIO_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN"
SCENARIO_VERSION = "001"
TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
LKM = "LKM SQL to Oracle (Built-In)"

_CAPABILITY_KEYS = {
    "status",
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "target_technology",
    "target_ikm",
    "target_ikm_installed",
    "target_ikm_assignable",
    "lkm",
    "scd2_role_assignment_supported",
}
_DATASTORE_KEYS = {
    "status",
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "source",
    "target",
    "target_keys",
    "scd_roles",
}
_OBJECT_PROBE_KEYS = {
    "status",
    "side_effects",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "mapping",
    "scenario",
}


def _tracked_attributes() -> list[str]:
    metadata = [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
    ]
    return list(build_dim_product_scd2_plan(metadata)["tracked_attributes"])


EXPECTED_SCD_ROLES = {
    "surrogate_key": ["PRODUCT_SK"],
    "natural_key": ["PROD_ID"],
    "start_date": ["EFFECTIVE_FROM"],
    "end_date": ["EFFECTIVE_TO"],
    "current_flag": ["CURRENT_FLAG"],
    "tracked_attributes": _tracked_attributes(),
}


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_dim_product_odi_scd2_packaging_readiness",
        "status": status,
        "source": dict(SOURCE),
        "target": dict(TARGET),
        "mapping": MAPPING_NAME,
        "scenario": {"name": SCENARIO_NAME, "version": SCENARIO_VERSION},
        "required_target_ikm": TARGET_IKM,
        "required_lkm": LKM,
        "requires_read_only_odi_capability_probe": True,
        "requires_separate_mapping_builder": True,
        "requires_independent_review": True,
        "requires_exact_operator_approval": True,
        "server_side_executor_remains_baseline": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_dim_product_odi_scd2_packaging_plan() -> dict[str, Any]:
    """Freeze the intended ODI package without claiming live capability."""
    return {
        **_base("blocked_pending_read_only_odi_capability_probe"),
        "expected_scd_roles": {
            key: list(value) for key, value in EXPECTED_SCD_ROLES.items()
        },
        "existing_generic_builder_reusable": False,
        "existing_generic_builder_reason": (
            "the current generic builder targets IU update-key merge semantics and "
            "does not assign the frozen SCD2 roles"
        ),
        "required_next": [
            "implement_metadata_only_odi_capability_probe",
            "independently_review_capability_probe",
            "collect_normalized_read_only_capability_evidence",
        ],
    }


def _safe_evidence(report: Mapping[str, Any], allowed_keys: set[str]) -> bool:
    return (
        set(report) == allowed_keys
        and report.get("side_effects") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
    )


def _identity_matches(
    value: object,
    expected: Mapping[str, object],
    *,
    column_count: int,
) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"owner", "table", "column_count"}
        and value.get("owner") == expected["owner"]
        and value.get("table") == expected["table"]
        and value.get("column_count") == column_count
    )


def _object_identity_matches(report: Mapping[str, Any]) -> bool:
    mapping = report.get("mapping")
    scenario = report.get("scenario")
    return (
        isinstance(mapping, Mapping)
        and set(mapping) == {"name", "exists"}
        and mapping.get("name") == MAPPING_NAME
        and isinstance(scenario, Mapping)
        and set(scenario) == {"name", "version", "exists"}
        and scenario.get("name") == SCENARIO_NAME
        and scenario.get("version") == SCENARIO_VERSION
    )


def build_dim_product_odi_scd2_packaging_readiness(
    capability_report: Mapping[str, Any],
    datastore_report: Mapping[str, Any],
    object_probe: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate normalized metadata evidence without opening a connection."""
    if capability_report.get("status") != "passed":
        return _base("blocked_capability_probe")
    if not _safe_evidence(capability_report, _CAPABILITY_KEYS):
        return _base("blocked_unsafe_capability_evidence")
    if capability_report.get("target_technology") != "ORACLE":
        return _base("blocked_target_technology")
    if capability_report.get("target_ikm_installed") is not True:
        return _base("blocked_scd2_ikm_missing")
    if capability_report.get("target_ikm_assignable") is not True:
        return _base("blocked_scd2_ikm_not_assignable")
    if capability_report.get("target_ikm") != TARGET_IKM:
        return _base("blocked_scd2_ikm_contract")
    if capability_report.get("lkm") != LKM:
        return _base("blocked_lkm_contract")
    if capability_report.get("scd2_role_assignment_supported") is not True:
        return _base("blocked_scd2_role_api")

    if datastore_report.get("status") != "passed":
        return _base("blocked_datastore_probe")
    if not _safe_evidence(datastore_report, _DATASTORE_KEYS):
        return _base("blocked_unsafe_datastore_evidence")
    if not _identity_matches(datastore_report.get("source"), SOURCE, column_count=24):
        return _base("blocked_source_datastore_contract")
    if not _identity_matches(datastore_report.get("target"), TARGET, column_count=26):
        return _base("blocked_target_datastore_contract")
    if datastore_report.get("target_keys") != {
        "primary_key_columns": ["PRODUCT_SK"],
        "natural_key_columns": ["PROD_ID"],
    }:
        return _base("blocked_target_key_contract")
    if datastore_report.get("scd_roles") != EXPECTED_SCD_ROLES:
        return _base("blocked_scd2_role_contract")

    if object_probe.get("status") != "attention_required":
        return _base("blocked_object_probe")
    if not _safe_evidence(object_probe, _OBJECT_PROBE_KEYS):
        return _base("blocked_unsafe_object_evidence")
    if not _object_identity_matches(object_probe):
        return _base("blocked_object_identity_contract")
    mapping = object_probe["mapping"]
    scenario = object_probe["scenario"]
    if mapping.get("exists") is True:
        return _base("blocked_mapping_already_exists")
    if mapping.get("exists") is not False:
        return _base("blocked_object_probe")
    if scenario.get("exists") is True:
        return _base("blocked_scenario_already_exists")
    if scenario.get("exists") is not False:
        return _base("blocked_object_probe")

    return {
        **_base("ready_for_separate_mapping_builder_implementation_review"),
        "mapping_collision_free": True,
        "scenario_collision_free": True,
        "target_ikm_verified": True,
        "scd2_roles_verified": True,
        "required_next": [
            "implement_dedicated_absent_only_scd2_mapping_builder",
            "independently_review_mapping_builder",
            "request_exact_mapping_creation_approval",
            "inspect_created_mapping_read_only",
            "implement_and_review_scenario_generation_separately",
            "request_separate_scenario_run_approval",
        ],
    }
