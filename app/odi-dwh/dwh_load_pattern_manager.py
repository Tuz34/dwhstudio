"""Static, connectionless planning contracts for ODI-68 load patterns."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from scd2_manager import PRODUCTS_SOURCE_METADATA

_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]*$")

_DEPARTMENTS_METADATA = (
    ("DEPARTMENT_ID", "NUMBER(4)", False),
    ("DEPARTMENT_NAME", "VARCHAR2(30)", False),
    ("MANAGER_ID", "NUMBER(6)", True),
    ("LOCATION_ID", "NUMBER(4)", True),
)

_SALES_METADATA = (
    ("PROD_ID", "NUMBER", False),
    ("CUST_ID", "NUMBER", False),
    ("TIME_ID", "DATE", False),
    ("CHANNEL_ID", "NUMBER", False),
    ("PROMO_ID", "NUMBER", False),
    ("QUANTITY_SOLD", "NUMBER", False),
    ("AMOUNT_SOLD", "NUMBER", False),
)

_PRODUCTS_TYPE1_TRACKED_ATTRIBUTES = tuple(
    name for name, _data_type, _nullable in PRODUCTS_SOURCE_METADATA if name != "PROD_ID"
)
PRODUCTS_ODS_AUDIT_METADATA = (
    ("ODS_CREATED_AT", "TIMESTAMP", False),
    ("ODS_UPDATED_AT", "TIMESTAMP", True),
)


def _normalize_type(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").upper())


def _type_matches(actual: str, expected: str) -> bool:
    if expected == "NUMBER":
        return actual == "NUMBER" or actual.startswith("NUMBER(")
    return actual == expected


def _metadata_drift(
    items: Iterable[Mapping[str, object]],
    expected: tuple[tuple[str, str, bool], ...],
    *,
    allowed_additional: tuple[tuple[str, str, bool], ...] = (),
) -> dict[str, list[str]]:
    actual: dict[str, str] = {}
    invalid: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip().upper()
        data_type = _normalize_type(item.get("data_type"))
        if not _IDENTIFIER.fullmatch(name) or not data_type or name in actual:
            invalid.append("INVALID_OR_DUPLICATE_METADATA_ENTRY")
            continue
        actual[name] = data_type

    expected_names = [name for name, _data_type, _nullable in expected]
    expected_set = set(expected_names)
    allowed_set = {name for name, _data_type, _nullable in allowed_additional}
    expected_types = {
        name: data_type for name, data_type, _nullable in (*expected, *allowed_additional)
    }
    return {
        "missing_columns": [name for name in expected_names if name not in actual],
        "unexpected_columns": sorted(set(actual) - expected_set - allowed_set) + invalid,
        "type_mismatches": [
            name
            for name in (*expected_names, *sorted(allowed_set))
            if name in actual and not _type_matches(actual[name], expected_types[name])
        ],
    }


def _base_plan(
    *,
    phase: str,
    source: Mapping[str, str],
    target: Mapping[str, str],
    drift: Mapping[str, list[str]],
) -> dict[str, Any]:
    has_drift = any(drift.values())
    return {
        "mode": "static_metadata_contract",
        "phase": phase,
        "status": "blocked_by_metadata_drift" if has_drift else "metadata_ready_for_review",
        "source": dict(source),
        "target": dict(target),
        "metadata_drift": dict(drift),
        "requires_user_approval": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_departments_scd1_plan(
    source_metadata: Iterable[Mapping[str, object]],
) -> dict[str, Any]:
    """Build the Phase 2 Type 1 merge contract without a connection."""
    drift = _metadata_drift(source_metadata, _DEPARTMENTS_METADATA)
    base = _base_plan(
        phase="phase_2_type_1_merge",
        source={"owner": "HR", "table": "DEPARTMENTS"},
        target={"owner": "DWH", "table": "ODS_HR_DEPARTMENTS"},
        drift=drift,
    )
    if base["status"] != "metadata_ready_for_review":
        return {**base, "proposed_persistent_operations": [], "required_next": ["review_metadata_drift"]}

    return {
        **base,
        "natural_key": ["DEPARTMENT_ID"],
        "update_key": ["DEPARTMENT_ID"],
        "tracked_attributes": ["DEPARTMENT_NAME", "MANAGER_ID", "LOCATION_ID"],
        "audit_columns": ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"],
        "implementation_route": "server_side_type1_merge_candidate",
        "proposed_persistent_operations": ["insert_missing_natural_keys", "refresh_changed_attributes"],
        "dq_gates": [
            "single_row_per_natural_key",
            "insert_update_unchanged_count_reconciliation",
            "required_column_metadata",
        ],
        "rerun_guard": {
            "same_input_action": "no_op",
            "duplicate_natural_keys_allowed": False,
            "state": "requires_read_only_live_preflight",
        },
        "backup_contract": {
            "required_before_persistent_action": True,
            "preserve_current_target": True,
            "restore_path_review_required": True,
            "fallback_path": "proven_full_load_path",
        },
        "required_next": [
            "review_static_contract",
            "capture_read_only_count_status_metadata_preflight",
            "record_backup_and_restore_evidence",
            "review_rerun_guard",
            "request_exact_operator_approval",
        ],
    }


def build_products_ods_scd1_plan(
    source_metadata: Iterable[Mapping[str, object]],
) -> dict[str, Any]:
    """Build the PRODUCTS current-state ODS contract before DIM history."""
    drift = _metadata_drift(source_metadata, PRODUCTS_SOURCE_METADATA)
    base = _base_plan(
        phase="phase_1a_products_ods_type_1",
        source={"owner": "SH", "table": "PRODUCTS"},
        target={"owner": "DWH", "table": "ODS_SH_PRODUCTS"},
        drift=drift,
    )
    if base["status"] != "metadata_ready_for_review":
        return {**base, "proposed_persistent_operations": [], "required_next": ["review_metadata_drift"]}

    return {
        **base,
        "natural_key": ["PROD_ID"],
        "update_key": ["PROD_ID"],
        "tracked_attributes": list(_PRODUCTS_TYPE1_TRACKED_ATTRIBUTES),
        "ods_audit_columns": ["ODS_CREATED_AT", "ODS_UPDATED_AT"],
        "history_preserved_in_ods": False,
        "downstream_history_target": {"owner": "DWH", "table": "DIM_PRODUCT"},
        "implementation_route": "server_side_type1_merge_candidate",
        "proposed_persistent_operations": ["insert_missing_products", "refresh_changed_product_attributes"],
        "dq_gates": [
            "single_row_per_product_natural_key",
            "insert_refresh_unchanged_count_reconciliation",
            "source_target_current_count_reconciliation",
            "required_product_column_metadata",
        ],
        "rerun_guard": {
            "same_input_action": "no_op",
            "duplicate_natural_keys_allowed": False,
            "history_rows_created_in_ods": False,
            "state": "requires_read_only_live_preflight",
        },
        "backup_contract": {
            "required_before_persistent_action": True,
            "preserve_current_ods_target": True,
            "restore_path_review_required": True,
            "downstream_dim_load_blocked_until_ods_proven": True,
        },
        "required_next": [
            "review_static_contract",
            "capture_read_only_ods_metadata_and_count_preflight",
            "record_backup_and_restore_evidence",
            "review_type1_rerun_guard",
            "request_exact_operator_approval",
        ],
    }


def build_products_ods_target_scd1_plan(
    target_metadata: Iterable[Mapping[str, object]],
) -> dict[str, Any]:
    """Validate the physical ODS target, including its required audit columns."""
    drift = _metadata_drift(
        target_metadata,
        (*PRODUCTS_SOURCE_METADATA, *PRODUCTS_ODS_AUDIT_METADATA),
    )
    return _base_plan(
        phase="phase_1a_products_ods_type_1_target",
        source={"owner": "SH", "table": "PRODUCTS"},
        target={"owner": "DWH", "table": "ODS_SH_PRODUCTS"},
        drift=drift,
    )


def build_sales_partition_plan(
    target_metadata: Iterable[Mapping[str, object]],
    *,
    estimated_row_count: object,
) -> dict[str, Any]:
    """Build the Phase 3 monthly-partition migration contract."""
    drift = _metadata_drift(target_metadata, _SALES_METADATA)
    base = _base_plan(
        phase="phase_3_partitioned_fact_load",
        source={"owner": "SH", "table": "SALES"},
        target={"owner": "DWH", "table": "ODS_SH_SALES"},
        drift=drift,
    )
    valid_count = isinstance(estimated_row_count, int) and not isinstance(estimated_row_count, bool)
    valid_count = valid_count and estimated_row_count >= 0
    if not valid_count:
        return {
            **base,
            "status": "blocked_by_invalid_count_metadata",
            "estimated_row_count": None,
            "proposed_persistent_operations": [],
            "required_next": ["capture_count_only_target_size"],
        }
    if base["status"] != "metadata_ready_for_review":
        return {
            **base,
            "estimated_row_count": estimated_row_count,
            "proposed_persistent_operations": [],
            "required_next": ["review_metadata_drift"],
        }

    return {
        **base,
        "estimated_row_count": estimated_row_count,
        "partition_key": "TIME_ID",
        "partition_grain": "monthly",
        "migration_route": "new_parallel_target_backfill_validate_operator_swap",
        "window_source": "existing_time_id_watermark_and_rerun_guard",
        "proposed_persistent_operations": [
            "prepare_parallel_partitioned_target",
            "backfill_and_count_reconcile",
            "operator_controlled_target_swap",
            "load_reviewed_window_partition",
        ],
        "dq_gates": [
            "source_target_total_count_reconciliation",
            "reviewed_window_count_reconciliation",
            "unaffected_partition_count_stability",
            "watermark_status_alignment",
        ],
        "rerun_guard": {
            "already_loaded_window_action": "replace_reviewed_partition_only",
            "touch_scope": "reviewed_window_partition_only",
            "full_target_reload_allowed": False,
            "state": "requires_read_only_live_preflight",
        },
        "backup_contract": {
            "required_before_persistent_action": True,
            "preserve_current_target": True,
            "parallel_target_required": True,
            "operator_swap_required": True,
            "restore_previous_target_and_watermark": True,
        },
        "required_next": [
            "review_static_contract",
            "capture_partition_capability_and_count_metadata",
            "record_backup_and_restore_evidence",
            "review_window_rerun_guard",
            "request_exact_operator_approval",
        ],
    }


def build_sales_soft_delete_plan(
    target_metadata: Iterable[Mapping[str, object]],
) -> dict[str, Any]:
    """Build the Phase 4 count-only detection and soft-delete contract."""
    items = tuple(target_metadata)
    soft_delete_metadata = (
        ("DELETED_FLAG", "VARCHAR2(1)", False),
        ("ODS_DELETED_AT", "TIMESTAMP", True),
    )
    drift = _metadata_drift(items, _SALES_METADATA, allowed_additional=soft_delete_metadata)
    base = _base_plan(
        phase="phase_4_delete_handling",
        source={"owner": "SH", "table": "SALES"},
        target={"owner": "DWH", "table": "ODS_SH_SALES"},
        drift=drift,
    )
    if base["status"] != "metadata_ready_for_review":
        return {**base, "proposed_persistent_operations": [], "required_next": ["review_metadata_drift"]}

    present_names = {str(item.get("name") or "").strip().upper() for item in items}
    present_soft_delete = present_names & {"DELETED_FLAG", "ODS_DELETED_AT"}
    if len(present_soft_delete) == 1:
        return {
            **base,
            "status": "blocked_by_partial_soft_delete_metadata",
            "soft_delete_columns": ["DELETED_FLAG", "ODS_DELETED_AT"],
            "proposed_persistent_operations": [],
            "required_next": ["review_partial_soft_delete_metadata"],
        }

    return {
        **base,
        "policy": "soft_delete",
        "hard_delete_allowed": False,
        "detection": "in_database_count_only_anti_join",
        "grain_columns": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"],
        "soft_delete_columns": ["DELETED_FLAG", "ODS_DELETED_AT"],
        "soft_delete_schema_change_required": not present_soft_delete,
        "candidate_count_field_included": True,
        "candidate_keys_included": False,
        "proposed_persistent_operations": [
            "add_soft_delete_metadata_columns_if_absent",
            "mark_counted_missing_source_records",
        ],
        "dq_gates": [
            "approved_delete_flag_values",
            "deleted_timestamp_consistency",
            "active_count_reconciliation",
            "repeat_apply_count_stability",
        ],
        "rerun_guard": {
            "already_marked_action": "no_op",
            "hard_delete_allowed": False,
            "state": "requires_count_only_candidate_preflight",
        },
        "backup_contract": {
            "required_before_persistent_action": True,
            "preserve_pre_apply_flag_counts": True,
            "restore_marked_records_by_reviewed_batch": True,
        },
        "required_next": [
            "review_static_contract",
            "capture_count_only_candidate_preflight",
            "record_backup_and_restore_evidence",
            "review_repeat_apply_guard",
            "request_exact_operator_approval",
        ],
    }


def build_supporting_patterns_plan() -> dict[str, Any]:
    """Keep Phase 5 patterns design-only until each receives a separate slice."""
    return {
        "mode": "static_metadata_contract",
        "phase": "phase_5_supporting_patterns",
        "status": "design_only_requires_separate_slices",
        "patterns": {
            "late_arriving_facts": {
                "candidate_policy": "park_pending_until_dimension_window_resolves",
                "dq_gate": "pending_and_resolved_count_reconciliation",
                "will_execute": False,
            },
            "early_arriving_dimensions": {
                "candidate_policy": "reviewed_inferred_member_then_attribute_refresh",
                "dq_gate": "inferred_member_status_count",
                "will_execute": False,
            },
            "periodic_snapshots": {
                "candidate_policy": "monthly_append_only_snapshot",
                "dq_gate": "single_snapshot_per_period_status",
                "will_execute": False,
            },
            "window_aggregate_refresh": {
                "candidate_policy": "refresh_reviewed_loaded_window_only",
                "dq_gate": "window_aggregate_count_and_total_status",
                "will_execute": False,
            },
        },
        "proposed_persistent_operations": [],
        "required_next": ["create_separate_reviewed_issue_per_selected_pattern"],
        "requires_user_approval": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def build_load_pattern_execution_request(
    plan: Mapping[str, object],
    *,
    approved: object,
    backup_status: str,
    preflight_status: str,
    rerun_status: str,
) -> dict[str, Any]:
    """Validate static gates while keeping execution unavailable."""
    base: dict[str, Any] = {
        "mode": "static_approval_gate",
        "approved": approved is True,
        "requires_user_approval": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    if plan.get("status") != "metadata_ready_for_review":
        return {**base, "status": "blocked_by_metadata_contract"}
    if approved is not True:
        return {**base, "status": "blocked_until_explicit_approval"}
    if backup_status != "ready":
        return {**base, "status": "blocked_pending_backup"}
    if preflight_status != "ready":
        return {**base, "status": "blocked_pending_read_only_preflight"}
    if rerun_status != "new_window_ready":
        return {**base, "status": "blocked_by_rerun_guard"}
    return {**base, "status": "blocked_pending_separate_executor_review"}
