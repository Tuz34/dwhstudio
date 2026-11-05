"""Fixed SDK-reference IKM import contract for ODI-68."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from dim_product_odi_scd2_packaging_readiness import (
    EXPECTED_SCD_ROLES,
    LKM,
    MAPPING_NAME,
    SCENARIO_NAME,
    SCENARIO_VERSION,
    SOURCE,
    TARGET,
    TARGET_IKM,
)

TOOL = "odi.dim_product_scd2_ikm_import"
RAW_MODE = "fixed_sdk_reference_absent_only_ikm_import"
NORMALIZED_MODE = "normalized_fixed_sdk_reference_absent_only_ikm_import"
SCRIPT_NAME = "dim_product_scd2_ikm_import.groovy"
REFERENCE_FILE_NAME = "KM_IKM Oracle Slowly Changing Dimension.xml"
TARGET_TECHNOLOGY = "ORACLE"
INTEGRATION_TYPE = "SLOWLY_CHANGING_DIMENSION"
IMPORT_MODE = "DUPLICATION"
APPROVAL_SCOPE = "odi68_dim_product_scd2_ikm_import"

_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
_ROOT_KEYS = {
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
_ERROR_KEYS = (_ROOT_KEYS - {"result"}) | {
    "error_type",
    "import_attempted",
    "repository_state_may_have_changed",
}
_RESULT_KEYS = {
    "status",
    "ikm_name",
    "target_technology",
    "integration_type",
    "import_mode",
    "existing_before",
    "import_attempted",
    "post_import_verified",
}


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_fixed_sdk_reference_absent_only_ikm_import",
        "status": status,
        "ikm_name": TARGET_IKM,
        "target_technology": TARGET_TECHNOLOGY,
        "integration_type": INTEGRATION_TYPE,
        "reference_file_name": REFERENCE_FILE_NAME,
        "import_mode": IMPORT_MODE,
        "approval_scope": APPROVAL_SCOPE,
        "absent_only": True,
        "replace_existing_ikm": False,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _safe_flags(report: Mapping[str, Any]) -> bool:
    return (
        report.get("side_effects") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
    )


def _probe_contract_matches(probe: Mapping[str, Any]) -> tuple[bool, str]:
    if (
        probe.get("status") != "passed"
        or probe.get("repository_connection_attempted") is not True
        or not _safe_flags(probe)
    ):
        return False, "blocked_capability_probe"
    capability = probe.get("capability")
    datastore = probe.get("datastore")
    objects = probe.get("objects")
    if not all(isinstance(value, Mapping) for value in (capability, datastore, objects)):
        return False, "blocked_capability_probe"
    if (
        capability.get("status") != "passed"
        or not _safe_flags(capability)
        or capability.get("target_technology") != TARGET_TECHNOLOGY
        or capability.get("lkm") != LKM
        or capability.get("scd2_role_assignment_supported") is not True
    ):
        return False, "blocked_capability_probe"
    if capability.get("target_ikm_installed") is True:
        return False, "blocked_ikm_already_exists"
    if (
        capability.get("target_ikm") is not None
        or capability.get("target_ikm_assignable") is not False
    ):
        return False, "blocked_ikm_state_mismatch"
    if (
        datastore.get("status") != "passed"
        or not _safe_flags(datastore)
        or datastore.get("source") != {**SOURCE, "column_count": 24}
        or datastore.get("target") != {**TARGET, "column_count": 26}
        or datastore.get("target_keys")
        != {
            "primary_key_columns": ["PRODUCT_SK"],
            "natural_key_columns": ["PROD_ID"],
        }
        or datastore.get("scd_roles") != EXPECTED_SCD_ROLES
    ):
        return False, "blocked_datastore_contract"
    if (
        objects.get("status") != "attention_required"
        or not _safe_flags(objects)
        or objects.get("mapping") != {"name": MAPPING_NAME, "exists": False}
        or objects.get("scenario")
        != {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "exists": False,
        }
    ):
        return False, "blocked_object_collision"
    return True, "ready"


def build_dim_product_odi_scd2_ikm_import_plan(
    capability_probe: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze an absent-only import of one installed Oracle SDK reference."""
    ready, status = _probe_contract_matches(capability_probe)
    if not ready:
        return _base(status)
    return {
        **_base("ready_for_importer_implementation_review"),
        "proposed_repository_operations": [
            "import_fixed_sdk_reference_ikm_in_duplication_mode",
            "verify_exact_ikm_name_technology_and_integration_type",
        ],
        "required_next": [
            "independently_review_import_contract",
            "independently_review_importer_implementation",
            "request_fresh_exact_import_approval",
            "run_fresh_read_only_capability_probe_after_import",
        ],
    }


def build_dim_product_odi_scd2_ikm_import_request(
    *,
    plan: Mapping[str, Any],
    capability_probe_review_status: object,
    import_contract_review_status: object,
    importer_implementation_review_status: object,
    approved: object,
    approval_scope: object,
) -> dict[str, Any]:
    """Apply every review and operator gate before a repository connection."""
    if plan.get("status") != "ready_for_importer_implementation_review":
        return _base("blocked_import_plan")
    if capability_probe_review_status != "approved":
        return _base("blocked_capability_probe_review")
    if import_contract_review_status != "approved":
        return _base("blocked_import_contract_review")
    if importer_implementation_review_status != "approved":
        return _base("blocked_importer_implementation_review")
    if approved is not True:
        return _base("blocked_until_explicit_approval")
    if approval_scope != APPROVAL_SCOPE:
        return _base("blocked_until_exact_approval_scope")
    return _base("ready_for_fixed_ikm_import_execution")


def _normalized_base(status: str, *, side_effects: bool | None) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "status": status,
        "mode": NORMALIZED_MODE,
        "side_effects": side_effects,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _invalid(error_type: str = "InvalidImportOutput") -> dict[str, Any]:
    safe_type = error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "InvalidImportOutput"
    return {
        **_normalized_base("blocked_invalid_import_output", side_effects=None),
        "repository_outcome": "unverified",
        "manual_recovery_review_required": True,
        "error": "ODI IKM import output was rejected.",
        "error_type": safe_type,
    }


def normalize_dim_product_odi_scd2_ikm_import(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only fixed status metadata from the importer."""
    try:
        if not isinstance(payload, Mapping):
            return _invalid()
        if payload.get("status") == "error":
            error_type = payload.get("error_type")
            attempted = payload.get("import_attempted")
            state_changed = payload.get("repository_state_may_have_changed")
            if (
                set(payload) != _ERROR_KEYS
                or payload.get("tool") != TOOL
                or payload.get("mode") != RAW_MODE
                or type(payload.get("side_effects")) is not bool
                or payload.get("repository_connection_attempted") is not True
                or payload.get("raw_rows_included") is not False
                or payload.get("pk_values_included") is not False
                or payload.get("secret_printed") is not False
                or type(attempted) is not bool
                or type(state_changed) is not bool
                or state_changed is not attempted
                or not isinstance(error_type, str)
                or not _ERROR_TYPE_RE.fullmatch(error_type)
            ):
                return _invalid()
            return {
                **_normalized_base("error", side_effects=state_changed),
                "error": "ODI IKM import failed.",
                "error_type": error_type,
                "import_attempted": attempted,
                "repository_state_may_have_changed": state_changed,
            }

        result = payload.get("result")
        if (
            set(payload) != _ROOT_KEYS
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
            or result.get("ikm_name") != TARGET_IKM
            or result.get("target_technology") != TARGET_TECHNOLOGY
            or result.get("integration_type") != INTEGRATION_TYPE
            or result.get("import_mode") != IMPORT_MODE
            or result.get("existing_before") is not False
            or result.get("import_attempted") is not True
            or result.get("post_import_verified") is not True
        ):
            return _invalid()
        return {
            **_normalized_base("passed", side_effects=True),
            "result": deepcopy(result),
        }
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _invalid(type(exc).__name__)

