from __future__ import annotations

from pathlib import Path

import pytest

from products_ods_preflight import build_products_ods_backup_restore_plan
from scd2_manager import PRODUCTS_SOURCE_METADATA


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _missing_target_preflight():
    return {
        "status": "blocked_target_missing",
        "source": {
            "exists": True,
            "metadata_status": "metadata_ready_for_review",
            "metadata_drift": {
                "missing_columns": [],
                "unexpected_columns": [],
                "type_mismatches": [],
            },
            "column_count": 22,
            "row_count": 72,
            "duplicate_key_group_count": 0,
        },
        "target": {
            "exists": False,
            "metadata_status": "target_missing",
            "row_count": None,
            "duplicate_key_group_count": None,
        },
        "raw_rows_included": False,
        "pk_values_included": False,
        "write_attempted": False,
    }


def _backup_plan(preflight=None):
    return build_products_ods_backup_restore_plan(
        preflight or _missing_target_preflight(),
        artifact_id="ODI68_PRODUCTS_ODS_20260713T153035",
    )


def test_initial_create_plan_freezes_exact_target_metadata_and_type1_rules():
    from products_ods_initial_create import build_products_ods_initial_create_plan

    plan = build_products_ods_initial_create_plan(_missing_target_preflight(), _backup_plan())

    assert plan["status"] == "ready_for_initial_create_review"
    assert plan["source"] == {"owner": "SH", "table": "PRODUCTS"}
    assert plan["target"] == {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
    assert plan["natural_key"] == ["PROD_ID"]
    assert plan["target_key_contract"] == {
        "column": "PROD_ID",
        "required": True,
        "unique": True,
    }
    assert plan["source_column_count"] == len(PRODUCTS_SOURCE_METADATA) == 22
    assert plan["target_column_count"] == 24
    columns = {item["name"]: item for item in plan["target_columns"]}
    assert columns["PROD_ID"]["data_type"] == "NUMBER(6)"
    assert columns["ODS_CREATED_AT"] == {
        "name": "ODS_CREATED_AT",
        "data_type": "TIMESTAMP",
        "nullable": False,
        "role": "insert_audit_timestamp",
    }
    assert columns["ODS_UPDATED_AT"] == {
        "name": "ODS_UPDATED_AT",
        "data_type": "TIMESTAMP",
        "nullable": True,
        "role": "change_audit_timestamp",
    }
    assert plan["history_preserved_in_ods"] is False
    assert plan["initial_load_evidence"] == [
        "source_count",
        "inserted_count",
        "target_count",
        "duplicate_natural_key_group_count",
        "metadata_status",
    ]


def test_initial_create_plan_fails_closed_when_preflight_is_not_missing_target():
    from products_ods_initial_create import build_products_ods_initial_create_plan

    preflight = _missing_target_preflight()
    preflight["status"] = "ready_for_named_backup"
    preflight["target"]["exists"] = True

    plan = build_products_ods_initial_create_plan(preflight, _backup_plan(preflight))

    assert plan["status"] == "blocked_target_state_changed"
    assert plan["proposed_persistent_operations"] == []
    assert plan["will_execute"] is False


def test_initial_create_plan_fails_closed_without_absent_target_baseline():
    from products_ods_initial_create import build_products_ods_initial_create_plan

    backup = _backup_plan()
    backup["status"] = "pending_named_live_backup"

    plan = build_products_ods_initial_create_plan(_missing_target_preflight(), backup)

    assert plan["status"] == "blocked_absent_target_baseline"
    assert plan["proposed_persistent_operations"] == []


@pytest.mark.parametrize(
    ("review_status", "approved", "cleanup_status", "expected"),
    [
        ("pending", True, "ready", "blocked_pending_independent_review"),
        ("approved", "yes", "ready", "blocked_until_exact_operator_approval"),
        ("approved", True, "missing", "blocked_pending_cleanup_restore_test"),
        ("approved", True, "ready", "blocked_pending_separate_executor_implementation"),
    ],
)
def test_initial_create_request_keeps_all_gates_fail_closed(
    review_status, approved, cleanup_status, expected
):
    from products_ods_initial_create import (
        build_products_ods_initial_create_plan,
        build_products_ods_initial_create_request,
    )

    plan = build_products_ods_initial_create_plan(_missing_target_preflight(), _backup_plan())
    request = build_products_ods_initial_create_request(
        plan,
        independent_review_status=review_status,
        approved=approved,
        cleanup_restore_test_status=cleanup_status,
    )

    assert request["status"] == expected
    assert request["approved"] is (approved is True)
    assert request["will_execute"] is False
    assert request["connection_attempted"] is False
    assert request["write_operations_attempted"] == []


def test_initial_create_doc_is_non_executable_and_honest_about_live_proof():
    text = (PROJECT_ROOT / "docs" / "dwh-products-ods-initial-create-contract.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()

    for required in (
        "initial target creation contract",
        "22 source columns",
        "ods_created_at",
        "ods_updated_at",
        "prod_id",
        "single current row",
        "absent-target baseline",
        "independent review",
        "exact operator approval",
        "count/status/metadata only",
        "no raw rows",
        "no pk values",
        "live restore has not been tested",
    ):
        assert required in lowered

    for forbidden in (
        "create table ",
        "insert into ",
        "merge into ",
        "delete from ",
        "truncate table ",
        "drop table ",
        "live-proven",
    ):
        assert forbidden not in lowered


def test_initial_create_module_has_no_connection_or_executable_sql_surface():
    source = (PROJECT_ROOT / "products_ods_initial_create.py").read_text(encoding="utf-8")
    lowered = source.lower()

    for forbidden in (
        "import oracledb",
        "get_connection",
        "create table",
        "insert into",
        "merge into",
        "delete from",
        "truncate table",
        "drop table",
    ):
        assert forbidden not in lowered
