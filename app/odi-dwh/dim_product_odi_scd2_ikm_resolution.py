"""Offline allowlisted resolution for the ODI-68 Oracle SCD2 IKM reference."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REQUIRED_IKM = "IKM Oracle Slowly Changing Dimension"
REQUIRED_TECHNOLOGY = "ORACLE"
REQUIRED_INTEGRATION_TYPE = "SLOWLY_CHANGING_DIMENSION"
REFERENCE_FILENAME = "KM_IKM Oracle Slowly Changing Dimension.xml"


def _base(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "mode": "offline_allowlisted_odi_sdk_reference_probe",
        "model_call_attempted": False,
        "repository_connection_attempted": False,
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def inspect_scd2_ikm_reference(reference_path: Path | str) -> dict[str, Any]:
    """Read only the allowlisted identity fields from one fixed SDK XML file."""
    path = Path(reference_path)
    if path.name != REFERENCE_FILENAME:
        return _base("blocked_reference_filename")
    try:
        root = ET.parse(path).getroot()
        values: dict[str, str] = {}
        for field in root.iter("Field"):
            name = field.get("name")
            if name in {"TrtName", "KmTechno", "IntgType"}:
                if name in values:
                    return _base("blocked_reference_identity")
                values[name] = str(field.text or "").strip()
    except (ET.ParseError, OSError, ValueError) as exc:
        return {
            **_base("blocked_reference_unavailable"),
            "error_type": type(exc).__name__,
        }

    if values != {
        "TrtName": REQUIRED_IKM,
        "KmTechno": REQUIRED_TECHNOLOGY,
        "IntgType": REQUIRED_INTEGRATION_TYPE,
    }:
        return _base("blocked_reference_identity")
    return {
        **_base("reference_ready_for_review"),
        "reference_filename": REFERENCE_FILENAME,
        "ikm_name": REQUIRED_IKM,
        "technology": REQUIRED_TECHNOLOGY,
        "integration_type": REQUIRED_INTEGRATION_TYPE,
    }


def _safe_capability(report: Mapping[str, Any]) -> bool:
    return (
        report.get("status") == "passed"
        and report.get("side_effects") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
        and report.get("lkm") == "LKM SQL to Oracle (Built-In)"
        and report.get("scd2_role_assignment_supported") is True
        and type(report.get("target_ikm_installed")) is bool
        and type(report.get("target_ikm_assignable")) is bool
    )


def _resolution_base(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "mode": "static_offline_scd2_ikm_resolution",
        "required_ikm": REQUIRED_IKM,
        "required_technology": REQUIRED_TECHNOLOGY,
        "required_integration_type": REQUIRED_INTEGRATION_TYPE,
        "official_reference_available": False,
        "repository_ikm_installed": False,
        "import_authorized": False,
        "will_execute": False,
        "model_call_attempted": False,
        "repository_connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_dim_product_odi_scd2_ikm_resolution_plan(
    *,
    repository_capability: Mapping[str, Any],
    installed_reference: Mapping[str, Any],
) -> dict[str, Any]:
    """Separate local template availability from repository installation state."""
    if not isinstance(installed_reference, Mapping) or (
        installed_reference.get("status") != "reference_ready_for_review"
        or installed_reference.get("reference_filename") != REFERENCE_FILENAME
        or installed_reference.get("ikm_name") != REQUIRED_IKM
        or installed_reference.get("technology") != REQUIRED_TECHNOLOGY
        or installed_reference.get("integration_type")
        != REQUIRED_INTEGRATION_TYPE
        or installed_reference.get("repository_connection_attempted") is not False
        or installed_reference.get("side_effects") is not False
        or installed_reference.get("raw_rows_included") is not False
        or installed_reference.get("pk_values_included") is not False
        or installed_reference.get("secret_printed") is not False
    ):
        return _resolution_base("blocked_installed_reference")
    if not isinstance(repository_capability, Mapping) or not _safe_capability(
        repository_capability
    ):
        return {
            **_resolution_base("blocked_repository_capability"),
            "official_reference_available": True,
        }

    installed = repository_capability.get("target_ikm_installed") is True
    assignable = repository_capability.get("target_ikm_assignable") is True
    reported_name = repository_capability.get("target_ikm")
    reported_technology = repository_capability.get("target_technology")
    if installed:
        if (
            reported_name != REQUIRED_IKM
            or reported_technology != REQUIRED_TECHNOLOGY
            or not assignable
        ):
            return {
                **_resolution_base("blocked_repository_ikm_mismatch"),
                "official_reference_available": True,
                "repository_ikm_installed": True,
            }
        return {
            **_resolution_base("repository_ikm_already_available"),
            "official_reference_available": True,
            "repository_ikm_installed": True,
            "required_next": [
                "run_fresh_capability_probe_after_target_datastore_registration",
                "review_mapping_builder_prerequisites",
            ],
        }
    if reported_name is not None or assignable:
        return {
            **_resolution_base("blocked_repository_ikm_mismatch"),
            "official_reference_available": True,
        }
    return {
        **_resolution_base("ready_for_separate_ikm_import_contract_review"),
        "official_reference_available": True,
        "repository_ikm_installed": False,
        "required_next": [
            "design_fixed_reference_import_contract",
            "independently_review_import_contract_and_implementation",
            "request_fresh_exact_operator_approval_for_repository_import",
            "run_fresh_read_only_capability_probe_after_import",
        ],
    }
