from pathlib import Path

import local_model_eval as eval_safety
import scd2_manager


def _product_source_metadata() -> list[dict[str, object]]:
    return [
        {"name": "PROD_ID", "data_type": "NUMBER(6)", "nullable": False},
        {"name": "PROD_NAME", "data_type": "VARCHAR2(50)", "nullable": False},
        {"name": "PROD_DESC", "data_type": "VARCHAR2(4000)", "nullable": False},
        {"name": "PROD_SUBCATEGORY", "data_type": "VARCHAR2(50)", "nullable": False},
        {"name": "PROD_SUBCATEGORY_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "PROD_SUBCATEGORY_DESC", "data_type": "VARCHAR2(2000)", "nullable": False},
        {"name": "PROD_CATEGORY", "data_type": "VARCHAR2(50)", "nullable": False},
        {"name": "PROD_CATEGORY_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "PROD_CATEGORY_DESC", "data_type": "VARCHAR2(2000)", "nullable": False},
        {"name": "PROD_WEIGHT_CLASS", "data_type": "NUMBER(3)", "nullable": False},
        {"name": "PROD_UNIT_OF_MEASURE", "data_type": "VARCHAR2(20)", "nullable": True},
        {"name": "PROD_PACK_SIZE", "data_type": "VARCHAR2(30)", "nullable": False},
        {"name": "SUPPLIER_ID", "data_type": "NUMBER(6)", "nullable": False},
        {"name": "PROD_STATUS", "data_type": "VARCHAR2(20)", "nullable": False},
        {"name": "PROD_LIST_PRICE", "data_type": "NUMBER(8,2)", "nullable": False},
        {"name": "PROD_MIN_PRICE", "data_type": "NUMBER(8,2)", "nullable": False},
        {"name": "PROD_TOTAL", "data_type": "VARCHAR2(13)", "nullable": False},
        {"name": "PROD_TOTAL_ID", "data_type": "NUMBER", "nullable": False},
        {"name": "PROD_SRC_ID", "data_type": "NUMBER", "nullable": True},
        {"name": "PROD_EFF_FROM", "data_type": "DATE", "nullable": True},
        {"name": "PROD_EFF_TO", "data_type": "DATE", "nullable": True},
        {"name": "PROD_VALID", "data_type": "VARCHAR2(1)", "nullable": True},
    ]


def test_dim_product_plan_freezes_metadata_and_stays_non_executable():
    plan = scd2_manager.build_dim_product_scd2_plan(_product_source_metadata())

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["source"] == {"owner": "DWH", "table": "ODS_SH_PRODUCTS"}
    assert plan["target"] == {"owner": "DWH", "table": "DIM_PRODUCT"}
    assert plan["natural_key"] == ["PROD_ID"]
    assert plan["tracked_attributes"] == [
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
    ]
    assert plan["source_columns_excluded_from_change_detection"] == [
        "PROD_ID",
        "PROD_SRC_ID",
        "PROD_EFF_FROM",
        "PROD_EFF_TO",
        "PROD_VALID",
        "ODS_CREATED_AT",
        "ODS_UPDATED_AT",
    ]
    assert plan["surrogate_key"] == {
        "column": "PRODUCT_SK",
        "data_type": "NUMBER",
        "generation": "sequence_per_row_version",
        "candidate_sequence": "DWH.SEQ_DIM_PRODUCT",
        "reuse_natural_key_map": False,
    }
    target_columns = {item["name"]: item["data_type"] for item in plan["target_columns"]}
    assert target_columns["PRODUCT_SK"] == "NUMBER"
    assert target_columns["PROD_ID"] == "NUMBER(6)"
    assert target_columns["EFFECTIVE_FROM"] == "DATE"
    assert target_columns["EFFECTIVE_TO"] == "DATE"
    assert target_columns["CURRENT_FLAG"] == "VARCHAR2(1)"
    assert target_columns["INSERT_DATE"] == "TIMESTAMP"
    assert target_columns["UPDATE_DATE"] == "TIMESTAMP"
    assert target_columns["SOURCE_SYSTEM"] == "VARCHAR2(50)"
    assert target_columns["ETL_BATCH_ID"] == "NUMBER"
    assert plan["implementation_route"] == "server_side_expire_then_insert_candidate"
    assert plan["proposed_persistent_operations"] == [
        "expire_changed_current_versions",
        "insert_new_current_versions",
    ]
    assert plan["rerun_guard"]["state"] == "requires_read_only_live_preflight"
    assert plan["rerun_guard"]["same_change_action"] == "no_op"
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False
    assert plan["raw_rows_included"] is False
    assert plan["secret_printed"] is False


def test_dim_product_plan_blocks_when_source_metadata_drifts():
    metadata = _product_source_metadata()
    metadata = [item for item in metadata if item["name"] != "PROD_CATEGORY"]
    metadata.append({"name": "UNREVIEWED_COLUMN", "data_type": "VARCHAR2(10)", "nullable": True})

    plan = scd2_manager.build_dim_product_scd2_plan(metadata)

    assert plan["status"] == "blocked_by_metadata_drift"
    assert plan["metadata_drift"] == {
        "missing_columns": ["PROD_CATEGORY"],
        "unexpected_columns": ["UNREVIEWED_COLUMN"],
        "type_mismatches": [],
    }
    assert plan["proposed_persistent_operations"] == []
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False


def test_dim_product_plan_blocks_when_source_type_changes():
    metadata = _product_source_metadata()
    metadata[0] = {"name": "PROD_ID", "data_type": "VARCHAR2(20)", "nullable": False}

    plan = scd2_manager.build_dim_product_scd2_plan(metadata)

    assert plan["status"] == "blocked_by_metadata_drift"
    assert plan["metadata_drift"]["type_mismatches"] == ["PROD_ID"]
    assert plan["proposed_persistent_operations"] == []


def test_dim_product_plan_accepts_reviewed_ods_audit_columns():
    metadata = [
        *_product_source_metadata(),
        {"name": "ODS_CREATED_AT", "data_type": "TIMESTAMP", "nullable": False},
        {"name": "ODS_UPDATED_AT", "data_type": "TIMESTAMP", "nullable": True},
    ]

    plan = scd2_manager.build_dim_product_scd2_plan(metadata)

    assert plan["status"] == "metadata_ready_for_review"
    assert plan["metadata_drift"] == {
        "missing_columns": [],
        "unexpected_columns": [],
        "type_mismatches": [],
    }


def test_dim_product_execution_request_requires_approval_and_live_preflight():
    plan = scd2_manager.build_dim_product_scd2_plan(_product_source_metadata())

    blocked = scd2_manager.build_dim_product_scd2_execution_request(plan, approved=False)
    assert blocked["status"] == "blocked_until_explicit_approval"
    assert blocked["will_execute"] is False
    assert blocked["write_operations_attempted"] == []
    assert blocked["connection_attempted"] is False

    approved = scd2_manager.build_dim_product_scd2_execution_request(plan, approved=True)
    assert approved["status"] == "blocked_pending_read_only_live_preflight"
    assert approved["will_execute"] is False
    assert approved["write_operations_attempted"] == []
    assert approved["connection_attempted"] is False
    assert approved["reason"] == "a separate read-only live preflight and reviewed rerun result are required"


def test_scd2_manager_contains_no_connection_or_executable_sql_surface():
    source = (Path(__file__).parents[1] / "scd2_manager.py").read_text(encoding="utf-8")
    lowered = source.lower()

    for forbidden in (
        "import oracledb",
        "get_connection",
        "create table",
        "insert into",
        "merge into",
        "update ",
        "delete from",
        "truncate",
    ):
        assert forbidden not in lowered


def test_dim_product_static_plan_doc_is_reviewable_and_non_executable():
    text = Path("docs/dwh-scd2-dim-product-static-plan.md").read_text(encoding="utf-8")
    lowered = text.lower()

    for required in (
        "non-executable static contract",
        "sequence per row version",
        "product_sk",
        "prod_id",
        "tracked business attributes",
        "source metadata exclusions",
        "metadata drift",
        "read-only live preflight",
        "rerun guard",
        "rollback",
        "explicit operator approval",
        "count/status/metadata only",
        "no raw rows",
        "no pk values",
    ):
        assert required in lowered

    for forbidden in (
        "create table",
        "insert into",
        "merge into",
        "update ",
        "delete from",
        "truncate",
        "live-proven",
    ):
        assert forbidden not in lowered
    eval_safety.assert_no_blocked_content(text)
