"""Static initial-target contract for the ODI-68 DIM_PRODUCT SCD2 flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dim_product_scd2_stock_contract import (
    PRE_MIGRATION_CONTRACT_ERA,
    PRE_MIGRATION_CURRENT_FLAG,
    PRE_MIGRATION_HISTORICAL_FLAG,
    PRE_MIGRATION_OPEN_ENDED_ISO,
    STOCK_CONTRACT_ERA,
    STOCK_CURRENT_FLAG,
    STOCK_HISTORICAL_FLAG,
    STOCK_OPEN_ENDED_ISO,
)
from scd2_manager import build_dim_product_scd2_plan

_SOURCE = {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
_TARGET = {"owner": "DWH", "table": "DIM_PRODUCT"}
_SEQUENCE = "DWH.SEQ_DIM_PRODUCT"
_APPROVAL_SCOPE = "odi68_dim_product_scd2_initial_create"


def _base(status: str) -> dict[str, Any]:
    return {
        "mode": "static_dim_product_initial_create_contract",
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


def _role(name: str) -> str:
    if name == "PRODUCT_SK":
        return "surrogate_key"
    if name == "PROD_ID":
        return "natural_key"
    if name in {"EFFECTIVE_FROM", "EFFECTIVE_TO", "CURRENT_FLAG"}:
        return "scd2_version_control"
    if name in {"INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"}:
        return "load_audit"
    return "tracked_attribute"


def build_dim_product_initial_create_plan(
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the absent DIM/sequence contract without opening a connection."""
    source = preflight.get("source")
    target = preflight.get("target")
    sequence = preflight.get("sequence")
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
        not isinstance(sequence, Mapping)
        or sequence.get("owner") != "DWH"
        or sequence.get("sequence") != "SEQ_DIM_PRODUCT"
        or sequence.get("exists") is not False
    ):
        return {**_base("blocked_sequence_state_changed"), "proposed_persistent_operations": []}

    source_metadata = source.get("metadata")
    if not isinstance(source_metadata, list):
        return {**_base("blocked_source_preflight"), "proposed_persistent_operations": []}
    scd2_plan = build_dim_product_scd2_plan(source_metadata)
    if scd2_plan.get("status") != "metadata_ready_for_review":
        return {**_base("blocked_source_metadata_drift"), "proposed_persistent_operations": []}

    target_columns = [
        {**column, "role": _role(str(column["name"]))}
        for column in scd2_plan["target_columns"]
    ]
    return {
        **_base("ready_for_initial_create_review"),
        "source_column_count": int(source.get("column_count") or 0),
        "target_column_count": len(target_columns),
        "target_columns": target_columns,
        "target_key_contract": {
            "column": "PRODUCT_SK",
            "required": True,
            "unique": True,
            "generation": "sequence_per_row_version",
        },
        "natural_key_contract": {
            "columns": ["PROD_ID"],
            "unique_across_history": False,
            "single_current_row_required": True,
        },
        "sequence": {
            "qualified_name": _SEQUENCE,
            "increment": 1,
            "cycle": False,
            "one_value_per_row_version": True,
        },
        "current_row_contract": {
            "column": "CURRENT_FLAG",
            "contract_era": STOCK_CONTRACT_ERA,
            "current_value": STOCK_CURRENT_FLAG,
            "historical_value": STOCK_HISTORICAL_FLAG,
            "open_ended_effective_to": STOCK_OPEN_ENDED_ISO,
            "maximum_current_rows_per_natural_key": 1,
        },
        "pre_migration_current_row_contract": {
            "column": "CURRENT_FLAG",
            "contract_era": PRE_MIGRATION_CONTRACT_ERA,
            "current_value": PRE_MIGRATION_CURRENT_FLAG,
            "historical_value": PRE_MIGRATION_HISTORICAL_FLAG,
            "open_ended_effective_to": PRE_MIGRATION_OPEN_ENDED_ISO,
            "accepted_by_load_preflight": False,
            "accepted_by": "dim_product_stock_migration_preflight_only",
        },
        "initial_state": "empty_target_and_absent_sequence_before_approved_setup",
        "proposed_persistent_operations": [
            "prepare_dim_product_sequence",
            "prepare_empty_dim_product_target",
            "enforce_surrogate_key_uniqueness",
        ],
        "rerun_guard": {
            "target_exists_action": "block_before_persistent_action",
            "sequence_exists_action": "block_before_persistent_action",
            "persistent_action_allowed": False,
        },
        "rollback_contract": {
            "baseline": "target_absent_and_sequence_absent",
            "disposable_target_and_sequence_proof_required": True,
            "cleanup_only_if_created_by_approved_run": True,
            "cleanup_requires_separate_approval": True,
            "ods_source_must_remain_unchanged": True,
            "live_restore_tested": False,
        },
        "approval_scope_candidate": _APPROVAL_SCOPE,
        "required_next": [
            "independent_review",
            "prepare_separate_disposable_proof_executor",
            "run_disposable_target_and_sequence_cleanup_proof_after_exact_approval",
            "prepare_separate_initial_create_executor",
            "request_exact_initial_create_approval",
        ],
    }


def build_dim_product_initial_create_request(
    plan: Mapping[str, Any],
    *,
    independent_review_status: str,
    approved: object,
    approval_scope: str,
    disposable_proof_status: str,
) -> dict[str, Any]:
    """Evaluate gates while keeping the persistent executor unavailable."""
    base = {
        "mode": "static_dim_product_initial_create_approval_gate",
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
    if approval_scope != _APPROVAL_SCOPE:
        return {**base, "status": "blocked_wrong_approval_scope"}
    if disposable_proof_status != "passed":
        return {**base, "status": "blocked_pending_disposable_proof"}
    return {**base, "status": "blocked_pending_separate_executor_implementation"}
