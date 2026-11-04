from __future__ import annotations

import json
from pathlib import Path

import local_model_eval as eval_safety
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _departments_metadata():
    return [
        {"name": "DEPARTMENT_ID", "data_type": "NUMBER(4)", "nullable": False},
        {"name": "DEPARTMENT_NAME", "data_type": "VARCHAR2(30)", "nullable": False},
        {"name": "MANAGER_ID", "data_type": "NUMBER(6)", "nullable": True},
        {"name": "LOCATION_ID", "data_type": "NUMBER(4)", "nullable": True},
    ]


def _sales_metadata():
    return [
        {"name": "PROD_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "CUST_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "TIME_ID", "data_type": "DATE", "nullable": False},
        {"name": "CHANNEL_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "PROMO_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "QUANTITY_SOLD", "data_type": "NUMBER", "nullable": False},
        {"name": "AMOUNT_SOLD", "data_type": "NUMBER", "nullable": False},
    ]


def _products_metadata():
    from scd2_manager import PRODUCTS_SOURCE_METADATA

    return [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
    ]


def test_products_type1_plan_keeps_ods_current_before_dim_history():
    from dwh_load_pattern_manager import build_products_ods_scd1_plan

    plan = build_products_ods_scd1_plan(_products_metadata())

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["source"] == {"owner": "SH", "table": "PRODUCTS"}
    assert plan["target"] == {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
    assert plan["natural_key"] == ["PROD_ID"]
    assert "PROD_NAME" in plan["tracked_attributes"]
    assert "PROD_VALID" in plan["tracked_attributes"]
    assert "PROD_ID" not in plan["tracked_attributes"]
    assert plan["history_preserved_in_ods"] is False
    assert plan["downstream_history_target"] == {"owner": "DWH", "table": "DIM_PRODUCT"}
    assert plan["rerun_guard"]["same_input_action"] == "no_op"
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False


def test_products_type1_plan_blocks_metadata_drift():
    from dwh_load_pattern_manager import build_products_ods_scd1_plan

    metadata = [item for item in _products_metadata() if item["name"] != "PROD_STATUS"]
    plan = build_products_ods_scd1_plan(metadata)

    assert plan["status"] == "blocked_by_metadata_drift"
    assert plan["metadata_drift"]["missing_columns"] == ["PROD_STATUS"]
    assert plan["proposed_persistent_operations"] == []


def test_products_type1_doc_freezes_ods_then_dim_order():
    text = (PROJECT_ROOT / "docs" / "dwh-products-ods-type1-contract.md").read_text(encoding="utf-8")
    lower = text.lower()

    assert "sh.products -> dwh.ods_sh_products" in lower
    assert "dwh.ods_sh_products -> dwh.dim_product" in lower
    assert "type 1" in lower
    assert "current state" in lower
    assert "does not authorize live work" in lower
    assert "no raw rows" in lower
    assert "no pk values" in lower


def test_phase2_scd1_plan_freezes_keys_attributes_backup_and_rerun_boundary():
    from dwh_load_pattern_manager import build_departments_scd1_plan

    plan = build_departments_scd1_plan(_departments_metadata())

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["source"] == {"owner": "HR", "table": "DEPARTMENTS"}
    assert plan["target"] == {"owner": "DWH", "table": "ODS_HR_DEPARTMENTS"}
    assert plan["natural_key"] == ["DEPARTMENT_ID"]
    assert plan["tracked_attributes"] == ["DEPARTMENT_NAME", "MANAGER_ID", "LOCATION_ID"]
    assert plan["audit_columns"] == ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"]
    assert plan["implementation_route"] == "server_side_type1_merge_candidate"
    assert plan["rerun_guard"]["same_input_action"] == "no_op"
    assert plan["backup_contract"]["required_before_persistent_action"] is True
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False


def test_phase2_metadata_drift_fails_closed():
    from dwh_load_pattern_manager import build_departments_scd1_plan

    plan = build_departments_scd1_plan(_departments_metadata()[:-1])

    assert plan["status"] == "blocked_by_metadata_drift"
    assert plan["proposed_persistent_operations"] == []
    assert plan["metadata_drift"]["missing_columns"] == ["LOCATION_ID"]


def test_phase3_partition_plan_uses_monthly_parallel_target_and_window_rerun():
    from dwh_load_pattern_manager import build_sales_partition_plan

    plan = build_sales_partition_plan(_sales_metadata(), estimated_row_count=918_000)

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["partition_key"] == "TIME_ID"
    assert plan["partition_grain"] == "monthly"
    assert plan["migration_route"] == "new_parallel_target_backfill_validate_operator_swap"
    assert plan["window_source"] == "existing_time_id_watermark_and_rerun_guard"
    assert plan["rerun_guard"]["touch_scope"] == "reviewed_window_partition_only"
    assert plan["backup_contract"]["preserve_current_target"] is True
    assert plan["estimated_row_count"] == 918_000
    assert plan["raw_rows_included"] is False


@pytest.mark.parametrize("estimated_row_count", [-1, True, "918000"])
def test_phase3_rejects_invalid_count_metadata(estimated_row_count):
    from dwh_load_pattern_manager import build_sales_partition_plan

    plan = build_sales_partition_plan(_sales_metadata(), estimated_row_count=estimated_row_count)

    assert plan["status"] == "blocked_by_invalid_count_metadata"
    assert plan["proposed_persistent_operations"] == []


def test_phase4_soft_delete_plan_is_count_only_and_never_hard_deletes():
    from dwh_load_pattern_manager import build_sales_soft_delete_plan

    plan = build_sales_soft_delete_plan(_sales_metadata())

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["policy"] == "soft_delete"
    assert plan["detection"] == "in_database_count_only_anti_join"
    assert plan["soft_delete_columns"] == ["DELETED_FLAG", "ODS_DELETED_AT"]
    assert plan["soft_delete_schema_change_required"] is True
    assert plan["hard_delete_allowed"] is False
    assert plan["rerun_guard"]["already_marked_action"] == "no_op"
    assert plan["candidate_keys_included"] is False
    assert plan["raw_rows_included"] is False


def test_phase4_accepts_complete_existing_soft_delete_metadata():
    from dwh_load_pattern_manager import build_sales_soft_delete_plan

    metadata = [
        *_sales_metadata(),
        {"name": "DELETED_FLAG", "data_type": "VARCHAR2(1)", "nullable": False},
        {"name": "ODS_DELETED_AT", "data_type": "TIMESTAMP", "nullable": True},
    ]
    plan = build_sales_soft_delete_plan(metadata)

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["soft_delete_schema_change_required"] is False


def test_phase4_partial_soft_delete_metadata_fails_closed():
    from dwh_load_pattern_manager import build_sales_soft_delete_plan

    metadata = [
        *_sales_metadata(),
        {"name": "DELETED_FLAG", "data_type": "VARCHAR2(1)", "nullable": False},
    ]
    plan = build_sales_soft_delete_plan(metadata)

    assert plan["status"] == "blocked_by_partial_soft_delete_metadata"
    assert plan["proposed_persistent_operations"] == []


def test_phase5_supporting_patterns_remain_design_only():
    from dwh_load_pattern_manager import build_supporting_patterns_plan

    plan = build_supporting_patterns_plan()

    assert plan["status"] == "design_only_requires_separate_slices"
    assert set(plan["patterns"]) == {
        "late_arriving_facts",
        "early_arriving_dimensions",
        "periodic_snapshots",
        "window_aggregate_refresh",
    }
    assert all(item["will_execute"] is False for item in plan["patterns"].values())
    assert plan["proposed_persistent_operations"] == []


@pytest.mark.parametrize(
    ("approved", "backup_status", "preflight_status", "rerun_status", "expected"),
    [
        ("yes", "ready", "ready", "new_window_ready", "blocked_until_explicit_approval"),
        (True, "missing", "ready", "new_window_ready", "blocked_pending_backup"),
        (True, "ready", "missing", "new_window_ready", "blocked_pending_read_only_preflight"),
        (True, "ready", "ready", "duplicate", "blocked_by_rerun_guard"),
        (True, "ready", "ready", "new_window_ready", "blocked_pending_separate_executor_review"),
    ],
)
def test_execution_request_stays_fail_closed_through_all_static_gates(
    approved, backup_status, preflight_status, rerun_status, expected
):
    from dwh_load_pattern_manager import build_departments_scd1_plan, build_load_pattern_execution_request

    plan = build_departments_scd1_plan(_departments_metadata())
    request = build_load_pattern_execution_request(
        plan,
        approved=approved,
        backup_status=backup_status,
        preflight_status=preflight_status,
        rerun_status=rerun_status,
    )

    assert request["status"] == expected
    assert request["will_execute"] is False
    assert request["connection_attempted"] is False
    assert request["write_operations_attempted"] == []


def test_plans_and_docs_contain_no_live_or_sensitive_evidence():
    from dwh_load_pattern_manager import (
        build_departments_scd1_plan,
        build_sales_partition_plan,
        build_sales_soft_delete_plan,
        build_supporting_patterns_plan,
    )

    plans = [
        build_departments_scd1_plan(_departments_metadata()),
        build_sales_partition_plan(_sales_metadata(), estimated_row_count=918_000),
        build_sales_soft_delete_plan(_sales_metadata()),
        build_supporting_patterns_plan(),
    ]
    for plan in plans:
        eval_safety.assert_no_blocked_content(json.dumps(plan))
        assert plan["will_execute"] is False
        assert plan["connection_attempted"] is False

    for filename in (
        "dwh-scd1-merge-static-plan.md",
        "dwh-partitioned-fact-contract.md",
        "dwh-delete-handling-contract.md",
        "dwh-supporting-patterns-contract.md",
    ):
        text = (PROJECT_ROOT / "docs" / filename).read_text(encoding="utf-8")
        eval_safety.assert_no_blocked_content(text)
        assert "does not authorize live work" in text.lower()
        assert "no raw rows" in text.lower()
        assert "no pk values" in text.lower()


def test_manager_has_no_connection_or_executable_sql_surface():
    source = (PROJECT_ROOT / "dwh_load_pattern_manager.py").read_text(encoding="utf-8").lower()

    for forbidden in (
        "import oracledb",
        "get_connection",
        "create table",
        "insert into",
        "merge into",
        "delete from",
        "truncate table",
    ):
        assert forbidden not in source
