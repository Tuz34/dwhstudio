from __future__ import annotations

from pathlib import Path

import pytest

from scd2_manager import PRODUCTS_SOURCE_METADATA


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ODS_AUDIT_METADATA = (
    ("ODS_CREATED_AT", "TIMESTAMP", False),
    ("ODS_UPDATED_AT", "TIMESTAMP", True),
)


def _source_metadata():
    return [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in (*PRODUCTS_SOURCE_METADATA, *ODS_AUDIT_METADATA)
    ]


def _missing_target_preflight():
    return {
        "status": "blocked_target_missing",
        "source": {
            "exists": True,
            "column_count": 24,
            "metadata": _source_metadata(),
            "metadata_status": "metadata_ready_for_review",
            "row_count": 72,
            "duplicate_key_group_count": 0,
        },
        "target": {
            "exists": False,
            "column_count": 0,
            "metadata_status": "target_missing",
            "row_count": None,
        },
        "sequence": {
            "owner": "DWH",
            "sequence": "SEQ_DIM_PRODUCT",
            "exists": False,
        },
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def test_initial_create_contract_freezes_dim_and_sequence_metadata():
    from dim_product_scd2_initial_create import build_dim_product_initial_create_plan

    plan = build_dim_product_initial_create_plan(_missing_target_preflight())

    assert plan["status"] == "ready_for_initial_create_review"
    assert plan["source"] == {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
    assert plan["target"] == {"owner": "DWH", "table": "DIM_PRODUCT"}
    assert plan["sequence"]["qualified_name"] == "DWH.SEQ_DIM_PRODUCT"
    assert plan["source_column_count"] == 24
    assert plan["target_column_count"] == 26
    assert plan["target_key_contract"] == {
        "column": "PRODUCT_SK",
        "required": True,
        "unique": True,
        "generation": "sequence_per_row_version",
    }
    assert plan["natural_key_contract"] == {
        "columns": ["PROD_ID"],
        "unique_across_history": False,
        "single_current_row_required": True,
    }
    columns = {column["name"]: column for column in plan["target_columns"]}
    assert columns["PRODUCT_SK"]["data_type"] == "NUMBER"
    assert columns["CURRENT_FLAG"]["data_type"] == "VARCHAR2(1)"
    assert columns["ETL_BATCH_ID"]["data_type"] == "NUMBER"
    assert "ODS_CREATED_AT" not in columns
    assert "ODS_UPDATED_AT" not in columns


def test_initial_create_contract_fails_closed_if_target_state_changed():
    from dim_product_scd2_initial_create import build_dim_product_initial_create_plan

    preflight = _missing_target_preflight()
    preflight["target"]["exists"] = True

    plan = build_dim_product_initial_create_plan(preflight)

    assert plan["status"] == "blocked_target_state_changed"
    assert plan["proposed_persistent_operations"] == []


def test_initial_create_contract_fails_closed_if_sequence_state_changed():
    from dim_product_scd2_initial_create import build_dim_product_initial_create_plan

    preflight = _missing_target_preflight()
    preflight["sequence"]["exists"] = True

    plan = build_dim_product_initial_create_plan(preflight)

    assert plan["status"] == "blocked_sequence_state_changed"
    assert plan["proposed_persistent_operations"] == []


@pytest.mark.parametrize(
    ("review_status", "approved", "scope", "proof_status", "expected"),
    [
        ("pending", True, "odi68_dim_product_scd2_initial_create", "passed", "blocked_pending_independent_review"),
        ("approved", "yes", "odi68_dim_product_scd2_initial_create", "passed", "blocked_until_exact_operator_approval"),
        ("approved", True, "wrong_scope", "passed", "blocked_wrong_approval_scope"),
        ("approved", True, "odi68_dim_product_scd2_initial_create", "missing", "blocked_pending_disposable_proof"),
        ("approved", True, "odi68_dim_product_scd2_initial_create", "passed", "blocked_pending_separate_executor_implementation"),
    ],
)
def test_initial_create_request_keeps_executor_unavailable(
    review_status, approved, scope, proof_status, expected
):
    from dim_product_scd2_initial_create import (
        build_dim_product_initial_create_plan,
        build_dim_product_initial_create_request,
    )

    request = build_dim_product_initial_create_request(
        build_dim_product_initial_create_plan(_missing_target_preflight()),
        independent_review_status=review_status,
        approved=approved,
        approval_scope=scope,
        disposable_proof_status=proof_status,
    )

    assert request["status"] == expected
    assert request["will_execute"] is False
    assert request["connection_attempted"] is False
    assert request["write_operations_attempted"] == []


def test_initial_create_doc_is_non_executable_and_honest_about_live_proof():
    text = (
        PROJECT_ROOT / "docs" / "dwh-dim-product-scd2-initial-create-contract.md"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    for required in (
        "dim_product initial target and sequence contract",
        "26 target columns",
        "seq_dim_product",
        "product_sk",
        "single current row",
        "absent-target baseline",
        "disposable proof",
        "independent review",
        "exact operator approval",
        "count/status/metadata only",
        "no raw rows",
        "no pk values",
        "live ddl has not been executed",
    ):
        assert required in lowered

    for forbidden in (
        "create table ",
        "create sequence ",
        "insert into ",
        "merge into ",
        "delete from ",
        "drop table ",
        "drop sequence ",
        "live-proven",
    ):
        assert forbidden not in lowered


def test_initial_create_module_has_no_connection_or_executable_sql_surface():
    source = (PROJECT_ROOT / "dim_product_scd2_initial_create.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()

    for forbidden in (
        "import oracledb",
        "get_connection",
        "create table",
        "create sequence",
        "insert into",
        "merge into",
        "delete from",
        "drop table",
        "drop sequence",
    ):
        assert forbidden not in lowered
