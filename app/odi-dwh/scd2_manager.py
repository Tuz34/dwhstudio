"""Static, non-executable SCD2 planning contracts for ODI-68."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]*$")

PRODUCTS_SOURCE_METADATA = (
    ("PROD_ID", "NUMBER(6)", False),
    ("PROD_NAME", "VARCHAR2(50)", False),
    ("PROD_DESC", "VARCHAR2(4000)", False),
    ("PROD_SUBCATEGORY", "VARCHAR2(50)", False),
    ("PROD_SUBCATEGORY_ID", "NUMBER", False),
    ("PROD_SUBCATEGORY_DESC", "VARCHAR2(2000)", False),
    ("PROD_CATEGORY", "VARCHAR2(50)", False),
    ("PROD_CATEGORY_ID", "NUMBER", False),
    ("PROD_CATEGORY_DESC", "VARCHAR2(2000)", False),
    ("PROD_WEIGHT_CLASS", "NUMBER(3)", False),
    ("PROD_UNIT_OF_MEASURE", "VARCHAR2(20)", True),
    ("PROD_PACK_SIZE", "VARCHAR2(30)", False),
    ("SUPPLIER_ID", "NUMBER(6)", False),
    ("PROD_STATUS", "VARCHAR2(20)", False),
    ("PROD_LIST_PRICE", "NUMBER(8,2)", False),
    ("PROD_MIN_PRICE", "NUMBER(8,2)", False),
    ("PROD_TOTAL", "VARCHAR2(13)", False),
    ("PROD_TOTAL_ID", "NUMBER", False),
    ("PROD_SRC_ID", "NUMBER", True),
    ("PROD_EFF_FROM", "DATE", True),
    ("PROD_EFF_TO", "DATE", True),
    ("PROD_VALID", "VARCHAR2(1)", True),
)

_ODS_AUDIT_METADATA = (
    ("ODS_CREATED_AT", "TIMESTAMP", False),
    ("ODS_UPDATED_AT", "TIMESTAMP", True),
)

_TRACKED_ATTRIBUTES = (
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
)

_EXCLUDED_FROM_CHANGE_DETECTION = (
    "PROD_ID",
    "PROD_SRC_ID",
    "PROD_EFF_FROM",
    "PROD_EFF_TO",
    "PROD_VALID",
)

_SCD2_COLUMNS = (
    ("EFFECTIVE_FROM", "DATE", False),
    ("EFFECTIVE_TO", "DATE", False),
    ("CURRENT_FLAG", "VARCHAR2(1)", False),
    ("INSERT_DATE", "TIMESTAMP", False),
    ("UPDATE_DATE", "TIMESTAMP", True),
    ("SOURCE_SYSTEM", "VARCHAR2(50)", False),
    ("ETL_BATCH_ID", "NUMBER", False),
)


def _normalize_type(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").upper())


def _normalize_source_metadata(
    source_metadata: Iterable[Mapping[str, object]],
) -> tuple[dict[str, dict[str, object]], bool]:
    normalized: dict[str, dict[str, object]] = {}
    invalid = False
    for item in source_metadata:
        name = str(item.get("name") or "").strip().upper()
        data_type = _normalize_type(item.get("data_type"))
        if not _IDENTIFIER.fullmatch(name) or not data_type or name in normalized:
            invalid = True
            continue
        normalized[name] = {
            "name": name,
            "data_type": data_type,
            "nullable": bool(item.get("nullable", True)),
        }
    return normalized, invalid


def _metadata_drift(
    actual: Mapping[str, Mapping[str, object]],
    *,
    invalid: bool,
) -> dict[str, list[str]]:
    expected_names = [name for name, _data_type, _nullable in PRODUCTS_SOURCE_METADATA]
    allowed_names = [name for name, _data_type, _nullable in _ODS_AUDIT_METADATA]
    expected_types = {
        name: data_type
        for name, data_type, _nullable in (*PRODUCTS_SOURCE_METADATA, *_ODS_AUDIT_METADATA)
    }
    actual_names = set(actual)
    expected_set = set(expected_names)
    missing = [name for name in expected_names if name not in actual_names]
    unexpected = sorted(actual_names - expected_set - set(allowed_names))
    mismatches = [
        name
        for name in (*expected_names, *allowed_names)
        if name in actual and actual[name]["data_type"] != expected_types[name]
    ]
    if invalid:
        unexpected.append("INVALID_OR_DUPLICATE_METADATA_ENTRY")
    return {
        "missing_columns": missing,
        "unexpected_columns": unexpected,
        "type_mismatches": mismatches,
    }


def _target_columns(actual: Mapping[str, Mapping[str, object]]) -> list[dict[str, object]]:
    business_names = ("PROD_ID", *_TRACKED_ATTRIBUTES)
    columns: list[dict[str, object]] = [
        {"name": "PRODUCT_SK", "data_type": "NUMBER", "nullable": False}
    ]
    columns.extend(
        {
            "name": name,
            "data_type": str(actual[name]["data_type"]),
            "nullable": bool(actual[name]["nullable"]),
        }
        for name in business_names
    )
    columns.extend(
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in _SCD2_COLUMNS
    )
    return columns


def build_dim_product_scd2_plan(
    source_metadata: Iterable[Mapping[str, object]],
) -> dict[str, Any]:
    """Build a static DIM_PRODUCT contract without opening a connection."""

    actual, invalid = _normalize_source_metadata(source_metadata)
    drift = _metadata_drift(actual, invalid=invalid)
    has_drift = any(drift.values())

    base: dict[str, Any] = {
        "mode": "static_metadata_contract",
        "status": "blocked_by_metadata_drift" if has_drift else "metadata_ready_for_review",
        "source": {"owner": "DWH", "table": "ODS_SH_PRODUCTS"},
        "target": {"owner": "DWH", "table": "DIM_PRODUCT"},
        "natural_key": ["PROD_ID"],
        "tracked_attributes": list(_TRACKED_ATTRIBUTES),
        "source_columns_excluded_from_change_detection": [
            *_EXCLUDED_FROM_CHANGE_DETECTION,
            "ODS_CREATED_AT",
            "ODS_UPDATED_AT",
        ],
        "surrogate_key": {
            "column": "PRODUCT_SK",
            "data_type": "NUMBER",
            "generation": "sequence_per_row_version",
            "candidate_sequence": "DWH.SEQ_DIM_PRODUCT",
            "reuse_natural_key_map": False,
        },
        "implementation_route": "server_side_expire_then_insert_candidate",
        "metadata_drift": drift,
        "dq_gates": [
            "effective_date_order",
            "approved_current_flag_values",
            "single_current_row_per_natural_key",
            "no_open_ended_historical_overlap",
            "count_status_metadata_reconciliation",
        ],
        "rerun_guard": {
            "state": "requires_read_only_live_preflight",
            "same_change_action": "no_op",
            "duplicate_current_versions_allowed": False,
            "persistent_action_allowed": False,
        },
        "rollback_contract": {
            "preserve_previous_target_state": True,
            "dq_failure_allows_commit": False,
            "fallback_path": "proven_full_load_path",
        },
        "requires_user_approval": True,
        "will_execute": False,
        "connection_attempted": False,
        "raw_rows_included": False,
        "secret_printed": False,
    }
    if has_drift:
        return {
            **base,
            "target_columns": [],
            "proposed_persistent_operations": [],
            "required_next": ["review_source_metadata_drift"],
        }
    return {
        **base,
        "target_columns": _target_columns(actual),
        "proposed_persistent_operations": [
            "expire_changed_current_versions",
            "insert_new_current_versions",
        ],
        "required_next": [
            "review_static_metadata_contract",
            "run_separate_read_only_live_preflight",
            "review_rerun_guard_result",
            "request_explicit_operator_approval_for_persistent_slice",
        ],
    }


def build_dim_product_scd2_execution_request(
    plan: Mapping[str, object],
    *,
    approved: bool,
) -> dict[str, Any]:
    """Keep the static contract blocked until a later live preflight slice."""

    base: dict[str, Any] = {
        "mode": "approval_gate",
        "approved": approved is True,
        "requires_user_approval": True,
        "will_execute": False,
        "write_operations_attempted": [],
        "connection_attempted": False,
        "raw_rows_included": False,
        "secret_printed": False,
    }
    if approved is not True:
        return {
            **base,
            "status": "blocked_until_explicit_approval",
            "reason": "explicit operator approval is required for any persistent SCD2 action",
        }
    if plan.get("status") != "metadata_ready_for_review":
        return {
            **base,
            "status": "blocked_by_metadata_drift",
            "reason": "the reviewed source metadata contract does not match",
        }
    return {
        **base,
        "status": "blocked_pending_read_only_live_preflight",
        "reason": "a separate read-only live preflight and reviewed rerun result are required",
    }
