"""Fixed absent-only ODI datastore registration contract for ODI-68."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from dim_product_odi_scd2_packaging_readiness import (
    EXPECTED_SCD_ROLES,
    MAPPING_NAME,
    SCENARIO_NAME,
    SCENARIO_VERSION,
    SOURCE,
    TARGET,
)
from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

TOOL = "odi.dim_product_scd2_datastore_registration"
RAW_MODE = "fixed_absent_only_odi_datastore_registration"
NORMALIZED_MODE = "normalized_fixed_absent_only_odi_datastore_registration"
SCRIPT_NAME = "dim_product_scd2_datastore_register.groovy"
MODEL_CODE = "TGT_DWH"
APPROVAL_SCOPE = "odi68_dim_product_odi_scd2_datastore_registration"
PRIMARY_KEY = {
    "name": "PK_DIM_PRODUCT",
    "columns": ["PRODUCT_SK"],
    "in_database": True,
}
NATURAL_KEY = {
    "name": "AK_DIM_PRODUCT_PROD_ID",
    "columns": ["PROD_ID"],
    "in_database": False,
}

_ODS_AUDIT_METADATA = (
    ("ODS_CREATED_AT", "TIMESTAMP", False),
    ("ODS_UPDATED_AT", "TIMESTAMP", True),
)
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
_SAFE_ROLE_COUNTS = {
    "surrogate_key": 1,
    "natural_key": 1,
    "start_date": 1,
    "end_date": 1,
    "current_flag": 1,
    "tracked_attributes": 17,
}
_SUCCESS_KEYS = {
    "tool",
    "status",
    "mode",
    "side_effects",
    "repository_connection_attempted",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "result",
}
_ERROR_KEYS = {
    "tool",
    "status",
    "mode",
    "side_effects",
    "repository_connection_attempted",
    "raw_rows_included",
    "pk_values_included",
    "secret_printed",
    "error_type",
}
_RESULT_KEYS = {
    "status",
    "model_code",
    "target_owner",
    "target_table",
    "target_column_count",
    "primary_key_columns",
    "natural_key_columns",
    "scd_role_column_counts",
    "commit_completed",
}


def _source_metadata() -> list[dict[str, object]]:
    return [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in (
            *PRODUCTS_SOURCE_METADATA,
            *_ODS_AUDIT_METADATA,
        )
    ]


def _target_columns() -> list[dict[str, object]]:
    plan = build_dim_product_scd2_plan(_source_metadata())
    if plan.get("status") != "metadata_ready_for_review":
        raise RuntimeError("Frozen DIM_PRODUCT metadata contract is invalid")
    return deepcopy(plan["target_columns"])


def _safe_flags(report: Mapping[str, Any]) -> bool:
    return (
        report.get("side_effects") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
    )


def _empty_target_metadata(capability_probe: Mapping[str, Any]) -> tuple[bool, str]:
    datastore = capability_probe.get("datastore")
    if not isinstance(datastore, Mapping) or not _safe_flags(datastore):
        return False, "blocked_capability_probe"
    source = datastore.get("source")
    target = datastore.get("target")
    keys = datastore.get("target_keys")
    roles = datastore.get("scd_roles")
    if (
        not isinstance(source, Mapping)
        or source.get("owner") != SOURCE["owner"]
        or source.get("table") != SOURCE["table"]
        or source.get("column_count") != 24
    ):
        return False, "blocked_source_datastore_contract"
    if (
        not isinstance(target, Mapping)
        or target.get("owner") != TARGET["owner"]
        or target.get("table") != TARGET["table"]
        or target.get("column_count") != 0
    ):
        return False, "blocked_target_datastore_not_absent"
    if keys != {"primary_key_columns": [], "natural_key_columns": []}:
        return False, "blocked_target_datastore_not_absent"
    if not isinstance(roles, Mapping) or set(roles) != set(EXPECTED_SCD_ROLES):
        return False, "blocked_capability_probe"
    if any(roles.get(role) != [] for role in EXPECTED_SCD_ROLES):
        return False, "blocked_target_datastore_not_absent"
    return True, "ready"


def _objects_absent(capability_probe: Mapping[str, Any]) -> bool:
    objects = capability_probe.get("objects")
    return (
        isinstance(objects, Mapping)
        and _safe_flags(objects)
        and objects.get("mapping")
        == {"name": MAPPING_NAME, "exists": False}
        and objects.get("scenario")
        == {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "exists": False,
        }
    )


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_fixed_absent_only_odi_datastore_registration",
        "status": status,
        "model_code": MODEL_CODE,
        "target": dict(TARGET),
        "approval_scope": APPROVAL_SCOPE,
        "absent_only": True,
        "replace_existing_datastore": False,
        "requires_user_approval": True,
        "requires_independent_review": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_dim_product_odi_scd2_datastore_registration_plan(
    capability_probe: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze exact target metadata from a reviewed absent-target probe."""
    if (
        not isinstance(capability_probe, Mapping)
        or capability_probe.get("status") != "passed"
        or not _safe_flags(capability_probe)
        or capability_probe.get("repository_connection_attempted") is not True
    ):
        return {**_base("blocked_capability_probe"), "target_columns": []}

    capability = capability_probe.get("capability")
    if (
        not isinstance(capability, Mapping)
        or not _safe_flags(capability)
        or capability.get("scd2_role_assignment_supported") is not True
    ):
        return {**_base("blocked_scd2_role_api"), "target_columns": []}

    target_ready, blocked_status = _empty_target_metadata(capability_probe)
    if not target_ready:
        return {**_base(blocked_status), "target_columns": []}
    if not _objects_absent(capability_probe):
        return {**_base("blocked_target_object_collision"), "target_columns": []}

    columns = _target_columns()
    return {
        **_base("ready_for_registration_implementation_review"),
        "target_column_count": len(columns),
        "target_columns": columns,
        "primary_key": deepcopy(PRIMARY_KEY),
        "natural_key": deepcopy(NATURAL_KEY),
        "scd_roles": {
            role: list(column_names)
            for role, column_names in EXPECTED_SCD_ROLES.items()
        },
        "proposed_repository_operations": [
            "create_absent_dim_product_datastore_metadata",
            "assign_frozen_scd2_column_roles",
            "create_primary_and_natural_key_metadata",
            "verify_exact_metadata_before_commit",
        ],
        "required_next": [
            "independently_review_registration_contract",
            "independently_review_dedicated_registrar",
            "request_fresh_exact_registration_approval",
            "run_fresh_read_only_capability_probe_after_registration",
        ],
    }


def build_dim_product_odi_scd2_datastore_registration_request(
    *,
    plan: Mapping[str, Any],
    capability_probe_review_status: object,
    registration_contract_review_status: object,
    registrar_implementation_review_status: object,
    approved: object,
    approval_scope: object,
) -> dict[str, Any]:
    """Evaluate all pre-connection gates for the dedicated registrar."""
    base = {
        **_base("blocked_registration_plan"),
        "approved": approved is True,
    }
    if plan.get("status") != "ready_for_registration_implementation_review":
        return base
    if capability_probe_review_status != "approved":
        return {**base, "status": "blocked_capability_probe_review"}
    if registration_contract_review_status != "approved":
        return {**base, "status": "blocked_registration_contract_review"}
    if registrar_implementation_review_status != "approved":
        return {**base, "status": "blocked_registrar_implementation_review"}
    if approved is not True:
        return {**base, "status": "blocked_until_explicit_approval"}
    if approval_scope != APPROVAL_SCOPE:
        return {**base, "status": "blocked_until_exact_approval_scope"}
    return {**base, "status": "ready_for_dedicated_registrar_execution"}


def _normalized_base(status: str) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "status": status,
        "mode": NORMALIZED_MODE,
        "side_effects": status == "passed",
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _invalid_output(error_type: str = "InvalidRegistrationOutput") -> dict[str, Any]:
    safe_error_type = (
        error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "InvalidRegistrationOutput"
    )
    return {
        **_normalized_base("blocked_invalid_registration_output"),
        "side_effects": None,
        "repository_outcome": "unverified",
        "manual_recovery_review_required": True,
        "error": "ODI datastore registration output was rejected.",
        "error_type": safe_error_type,
    }


def normalize_dim_product_odi_scd2_datastore_registration(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept only fixed count/status metadata from the dedicated registrar."""
    try:
        if not isinstance(payload, Mapping):
            return _invalid_output()
        if payload.get("status") in {"error", "outcome_unknown", "committed_report_incomplete"}:
            from repository_write_contract import failure
            state = failure(payload, TOOL, RAW_MODE)
            if state is None:
                return _invalid_output()
            return {
                **_normalized_base(payload["status"]), **state,
                "error": "ODI datastore registration did not produce a verified success report.",
            }

        result = payload.get("result")
        if (
            set(payload) != _SUCCESS_KEYS
            or payload.get("tool") != TOOL
            or payload.get("status") != "passed"
            or payload.get("mode") != RAW_MODE
            or payload.get("side_effects") is not True
            or payload.get("repository_connection_attempted") is not True
            or payload.get("raw_rows_included") is not False
            or payload.get("pk_values_included") is not False
            or payload.get("secret_printed") is not False
            or not isinstance(result, Mapping)
            or set(result) != _RESULT_KEYS
            or result.get("status") != "passed"
            or result.get("model_code") != MODEL_CODE
            or result.get("target_owner") != TARGET["owner"]
            or result.get("target_table") != TARGET["table"]
            or type(result.get("target_column_count")) is not int
            or result.get("target_column_count") != 26
            or result.get("primary_key_columns") != ["PRODUCT_SK"]
            or result.get("natural_key_columns") != ["PROD_ID"]
            or result.get("scd_role_column_counts") != _SAFE_ROLE_COUNTS
            or result.get("commit_completed") is not True
        ):
            return _invalid_output()
        return {**_normalized_base("passed"), "result": deepcopy(result)}
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid_output(type(exc).__name__)
