"""Static initial-target contract for the ODI-68 PRODUCTS ODS flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dwh_load_pattern_manager import PRODUCTS_ODS_AUDIT_METADATA
from scd2_manager import PRODUCTS_SOURCE_METADATA

_SOURCE = {"owner": "SH", "table": "PRODUCTS"}
_TARGET = {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
_ODS_AUDIT_ROLES = {
    "ODS_CREATED_AT": "insert_audit_timestamp",
    "ODS_UPDATED_AT": "change_audit_timestamp",
}


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_initial_target_contract",
        "status": status,
        "source": dict(_SOURCE),
        "target": dict(_TARGET),
        "requires_user_approval": True,
        "requires_independent_review": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _target_columns() -> list[dict[str, Any]]:
    columns = [
        {
            "name": name,
            "data_type": data_type,
            "nullable": nullable,
            "role": "natural_key" if name == "PROD_ID" else "source_current_state",
        }
        for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
    ]
    columns.extend(
        {
            "name": name,
            "data_type": data_type,
            "nullable": nullable,
            "role": _ODS_AUDIT_ROLES[name],
        }
        for name, data_type, nullable in PRODUCTS_ODS_AUDIT_METADATA
    )
    return columns


def build_products_ods_initial_create_plan(
    preflight: Mapping[str, Any],
    backup_restore_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the missing-target contract without creating any database object."""
    source = preflight.get("source")
    target = preflight.get("target")
    if not isinstance(source, Mapping) or source.get("metadata_status") != "metadata_ready_for_review":
        return {**_base("blocked_source_preflight"), "proposed_persistent_operations": []}
    if source.get("duplicate_key_group_count") != 0:
        return {**_base("blocked_source_duplicate_keys"), "proposed_persistent_operations": []}
    if (
        preflight.get("status") != "blocked_target_missing"
        or not isinstance(target, Mapping)
        or target.get("exists") is not False
    ):
        return {**_base("blocked_target_state_changed"), "proposed_persistent_operations": []}
    if (
        backup_restore_plan.get("status") != "static_restore_contract_ready"
        or backup_restore_plan.get("previous_state") != "target_absent"
        or backup_restore_plan.get("live_backup_created") is not False
        or backup_restore_plan.get("live_restore_tested") is not False
    ):
        return {**_base("blocked_absent_target_baseline"), "proposed_persistent_operations": []}

    columns = _target_columns()
    return {
        **_base("ready_for_initial_create_review"),
        "source_column_count": len(PRODUCTS_SOURCE_METADATA),
        "target_column_count": len(columns),
        "target_columns": columns,
        "natural_key": ["PROD_ID"],
        "target_key_contract": {
            "column": "PROD_ID",
            "required": True,
            "unique": True,
        },
        "history_preserved_in_ods": False,
        "initial_state": "empty_target_before_approved_type1_load",
        "proposed_persistent_operations": [
            "prepare_empty_ods_target",
            "enforce_single_current_row_per_product",
            "record_insert_and_change_audit_timestamps",
        ],
        "initial_load_evidence": [
            "source_count",
            "inserted_count",
            "target_count",
            "duplicate_natural_key_group_count",
            "metadata_status",
        ],
        "rerun_guard": {
            "same_source_action": "no_op_after_type1_sync",
            "duplicate_natural_keys_allowed": False,
            "history_rows_created_in_ods": False,
            "state": "requires_separate_live_rerun_proof",
        },
        "rollback_contract": {
            "baseline": "target_absent",
            "cleanup_only_if_created_by_approved_run": True,
            "downstream_dim_must_remain_unpromoted": True,
            "cleanup_requires_separate_approval": True,
            "live_restore_tested": False,
        },
        "required_next": [
            "independent_review",
            "prepare_separate_executor",
            "run_disposable_cleanup_restore_test_after_exact_approval",
            "request_exact_initial_create_approval",
        ],
    }


def build_products_ods_initial_create_request(
    plan: Mapping[str, Any],
    *,
    independent_review_status: str,
    approved: object,
    cleanup_restore_test_status: str,
) -> dict[str, Any]:
    """Evaluate gates while keeping the persistent executor unavailable."""
    base = {
        "mode": "static_initial_target_approval_gate",
        "approved": approved is True,
        "requires_user_approval": True,
        "will_execute": False,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    if plan.get("status") != "ready_for_initial_create_review":
        return {**base, "status": "blocked_initial_create_contract"}
    if independent_review_status != "approved":
        return {**base, "status": "blocked_pending_independent_review"}
    if approved is not True:
        return {**base, "status": "blocked_until_exact_operator_approval"}
    if cleanup_restore_test_status != "ready":
        return {**base, "status": "blocked_pending_cleanup_restore_test"}
    return {**base, "status": "blocked_pending_separate_executor_implementation"}
