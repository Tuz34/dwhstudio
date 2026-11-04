from __future__ import annotations

import pytest

import scd2_builder
from scd2_builder import Scd2PlanError, build_scd2_plan
from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

TRACKED_17 = [
    "PROD_NAME", "PROD_DESC", "PROD_SUBCATEGORY", "PROD_SUBCATEGORY_ID",
    "PROD_SUBCATEGORY_DESC", "PROD_CATEGORY", "PROD_CATEGORY_ID", "PROD_CATEGORY_DESC",
    "PROD_WEIGHT_CLASS", "PROD_UNIT_OF_MEASURE", "PROD_PACK_SIZE", "SUPPLIER_ID",
    "PROD_STATUS", "PROD_LIST_PRICE", "PROD_MIN_PRICE", "PROD_TOTAL", "PROD_TOTAL_ID",
]


def products_columns():
    return [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
    ]


def small_columns():
    return [
        {"name": "CUST_ID", "data_type": "NUMBER(10)", "nullable": False},
        {"name": "CUST_NAME", "data_type": "VARCHAR2(50)", "nullable": False},
        {"name": "CUST_CITY", "data_type": "VARCHAR2(30)", "nullable": True},
        {"name": "CREDIT_LIMIT", "data_type": "NUMBER(8,2)", "nullable": True},
        {"name": "UPDATED_ON", "data_type": "TIMESTAMP(6)", "nullable": True},
    ]


def request(**overrides):
    base = {
        "source_owner": "DWH",
        "source_table": "ODS_CUSTOMERS",
        "natural_key": ["CUST_ID"],
        "tracked_columns": ["CUST_NAME", "CUST_CITY"],
        "target_owner": "DM",
        "target_table": "DIM_CUSTOMER",
        "mapping_name": "MAP_ODS_CUST_TO_DIM_CUST",
    }
    base.update(overrides)
    return base


def test_plan_matches_the_frozen_dim_product_26_column_contract():
    plan = build_scd2_plan(
        {
            "source_owner": "DWH",
            "source_table": "ODS_SH_PRODUCTS",
            "natural_key": ["PROD_ID"],
            "tracked_columns": TRACKED_17,
            "target_owner": "DWH",
            "target_table": "DIM_PRODUCT",
            "mapping_name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
        },
        products_columns(),
    )
    frozen = build_dim_product_scd2_plan(
        [*products_columns(), {"name": "ODS_CREATED_AT", "data_type": "TIMESTAMP", "nullable": False},
         {"name": "ODS_UPDATED_AT", "data_type": "TIMESTAMP", "nullable": True}]
    )
    expected = [(c["name"], c["data_type"], c["nullable"]) for c in frozen["target_columns"]]
    assert scd2_builder.expected_target_metadata(plan) == expected
    assert len(plan["target_columns"]) == 26
    assert plan["surrogate_key"]["column"] == "PRODUCT_SK"
    assert plan["surrogate_key"]["sequence"] == "SEQ_DIM_PRODUCT"


def test_plan_contract_roles_and_connectionless_flags():
    plan = build_scd2_plan(request(), small_columns())
    roles = {c["name"]: c["scd_role"] for c in plan["target_columns"]}
    assert roles["CUSTOMER_SK"] == "SURROGATE_KEY"
    assert roles["CUST_ID"] == "NATURAL_KEY"
    assert roles["CUST_NAME"] == roles["CUST_CITY"] == "ADD_ROW_ON_CHANGE"
    assert roles["EFFECTIVE_FROM"] == "START_TIMESTAMP"
    assert roles["EFFECTIVE_TO"] == "END_TIMESTAMP"
    assert roles["CURRENT_FLAG"] == "CURRENT_RECORD_FLAG"
    assert "CREDIT_LIMIT" not in roles  # untracked source columns stay out of the target
    assert plan["connection_attempted"] is False
    assert plan["write_operations_attempted"] == []
    assert plan["raw_rows_included"] is False
    assert plan["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN"
    assert plan["models"] == {"source": "TGT_DWH", "target": "TGT_DM"}
    assert plan["knowledge_modules"]["ikm"] == "IKM Oracle Slowly Changing Dimension"
    assert plan["primary_key"]["columns"] == ["CUSTOMER_SK"]
    assert plan["alternate_key"]["columns"] == ["CUST_ID"]


def test_tracked_all_takes_every_non_key_source_column():
    plan = build_scd2_plan(request(tracked_columns="all"), small_columns())
    assert plan["tracked_attributes"] == ["CUST_NAME", "CUST_CITY", "CREDIT_LIMIT", "UPDATED_ON"]
    types = {c["name"]: c["data_type"] for c in plan["target_columns"]}
    assert types["UPDATED_ON"] == "TIMESTAMP"  # TIMESTAMP(6) normalised
    assert types["CREDIT_LIMIT"] == "NUMBER(8,2)"


def test_overwrite_columns_become_type1_attributes():
    plan = build_scd2_plan(
        request(tracked_columns=["CUST_NAME"], overwrite_columns=["CUST_CITY"]), small_columns()
    )
    roles = {c["name"]: c["scd_role"] for c in plan["target_columns"]}
    assert roles["CUST_CITY"] == "OVERWRITE_ON_CHANGE"
    assert plan["overwrite_attributes"] == ["CUST_CITY"]
    params = scd2_builder.groovy_params(plan)
    assert params["odi.scd2.overwrite"] == "CUST_CITY"
    assert scd2_builder.groovy_params(build_scd2_plan(request(), small_columns()))[
        "odi.scd2.overwrite"
    ] == "none"


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"target_owner": "ODS"}, "target_owner_not_allowed"),
        ({"target_owner": "SYS"}, "target_owner_not_allowed"),
        ({"source_owner": "SYS"}, "source_owner_not_allowed"),
        ({"target_table": "DIM CUST"}, "invalid_target_table"),
        ({"target_table": "X" * 31}, "invalid_target_table"),
        ({"target_table": 'DIM";DROP TABLE X--'}, "invalid_target_table"),
        ({"source_table": ""}, "invalid_source_table"),
        ({"mapping_name": "map-1"}, "invalid_mapping_name"),
        ({"mapping_name": "M" * 32}, "mapping_name_too_long"),
        ({"natural_key": []}, "invalid_natural_key"),
        ({"natural_key": "CUST_ID"}, "invalid_natural_key"),
        ({"natural_key": ["CUST_ID", "CUST_ID"]}, "invalid_natural_key_duplicate"),
        ({"tracked_columns": "some"}, "invalid_tracked_columns"),
        ({"tracked_columns": []}, "invalid_tracked_columns"),
        ({"sequence_name": "SEQ BAD"}, "invalid_sequence_name"),
        ({"natural_key": ["CUST$ID"]}, "invalid_natural_key_unsupported_character"),
    ],
)
def test_request_validation_rejects_bad_identifiers_and_owners(overrides, code):
    with pytest.raises(Scd2PlanError) as excinfo:
        build_scd2_plan(request(**overrides), small_columns())
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"natural_key": ["MISSING_ID"]}, "natural_key_not_in_source"),
        ({"tracked_columns": ["NOPE"]}, "tracked_not_in_source"),
        ({"tracked_columns": ["CUST_ID", "CUST_NAME"]}, "tracked_overlaps_natural_key"),
        ({"source_owner": "DM", "source_table": "DIM_CUSTOMER"}, "source_equals_target"),
    ],
)
def test_natural_key_and_tracked_must_be_subset_of_source(overrides, code):
    with pytest.raises(Scd2PlanError) as excinfo:
        build_scd2_plan(request(**overrides), small_columns())
    assert excinfo.value.code == code


def test_reserved_and_lob_columns_are_rejected():
    cols = small_columns() + [{"name": "CURRENT_FLAG", "data_type": "VARCHAR2(1)", "nullable": True}]
    with pytest.raises(Scd2PlanError) as excinfo:
        build_scd2_plan(request(tracked_columns=["CURRENT_FLAG"]), cols)
    assert excinfo.value.code == "reserved_column_name"
    cols = small_columns() + [{"name": "NOTES", "data_type": "CLOB", "nullable": True}]
    with pytest.raises(Scd2PlanError) as excinfo:
        build_scd2_plan(request(tracked_columns=["NOTES"]), cols)
    assert excinfo.value.code == "unsupported_column_type"


def test_empty_source_columns_are_refused():
    with pytest.raises(Scd2PlanError) as excinfo:
        build_scd2_plan(request(), [])
    assert excinfo.value.code == "source_columns_required"


def test_create_table_ddl_text_is_26_style_and_has_no_drop():
    plan = build_scd2_plan(request(), small_columns())
    ddl = scd2_builder.render_create_table(plan)
    assert ddl.startswith('CREATE TABLE "DM"."DIM_CUSTOMER" (')
    assert '"CUSTOMER_SK" NUMBER NOT NULL' in ddl
    assert '"CUST_ID" NUMBER(10) NOT NULL' in ddl
    assert '"CUST_NAME" VARCHAR2(50) NOT NULL' in ddl  # tracked nullability follows the source
    assert '"CUST_CITY" VARCHAR2(30),' in ddl
    assert '"EFFECTIVE_FROM" DATE NOT NULL' in ddl
    assert '"EFFECTIVE_TO" DATE NOT NULL' in ddl
    assert '"CURRENT_FLAG" VARCHAR2(1) NOT NULL' in ddl
    assert '"INSERT_DATE" TIMESTAMP NOT NULL' in ddl
    assert '"UPDATE_DATE" TIMESTAMP' in ddl and '"UPDATE_DATE" TIMESTAMP NOT NULL' not in ddl
    assert '"SOURCE_SYSTEM" VARCHAR2(50) NOT NULL' in ddl
    assert '"ETL_BATCH_ID" NUMBER NOT NULL' in ddl
    assert 'CONSTRAINT "PK_DIM_CUSTOMER" PRIMARY KEY ("CUSTOMER_SK")' in ddl
    assert "DROP" not in ddl.upper()


def test_sequence_and_grants_text_are_odi_exec_only_by_default():
    plan = build_scd2_plan(request(), small_columns())
    assert scd2_builder.render_create_sequence(plan) == (
        'CREATE SEQUENCE "DM"."SEQ_DIM_CUSTOMER" START WITH 1 INCREMENT BY 1 NOCYCLE'
    )
    grants = scd2_builder.render_grant_statements(plan)
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert f'GRANT {privilege} ON "DM"."DIM_CUSTOMER" TO ODI_EXEC' in grants
    assert 'GRANT SELECT ON "DM"."SEQ_DIM_CUSTOMER" TO ODI_EXEC' in grants
    assert len(grants) == 5
    assert not any("APP_USER" in g or "DROP" in g or "REVOKE" in g for g in grants)


def test_app_user_reader_grant_is_explicit_opt_in():
    plan = build_scd2_plan(request(grant_select_to_app_user=True), small_columns())
    assert plan["options"]["grant_select_to_app_user"] is True
    assert 'GRANT SELECT ON "DM"."DIM_CUSTOMER" TO APP_USER' in scd2_builder.render_grant_statements(plan)
    with pytest.raises(Scd2PlanError):
        build_scd2_plan(request(grant_select_to_app_user="yes"), small_columns())


def test_char_semantics_types_flow_through_plan_ddl_and_params():
    cols = small_columns() + [{"name": "CUST_NOTE", "data_type": "VARCHAR2(40 CHAR)", "nullable": True}]
    plan = build_scd2_plan(request(tracked_columns=["CUST_NOTE"]), cols)
    assert '"CUST_NOTE" VARCHAR2(40 CHAR)' in scd2_builder.render_create_table(plan)
    assert "CUST_NOTE:VARCHAR2_40_CHAR" in scd2_builder.groovy_params(plan)["odi.scd2.column_types"]


def test_custom_sequence_name_is_used():
    plan = build_scd2_plan(request(sequence_name="seq_cust_sk"), small_columns())
    assert plan["surrogate_key"]["sequence"] == "SEQ_CUST_SK"
    assert scd2_builder.odi_expressions(plan)["CUSTOMER_SK"] == "DM.SEQ_CUST_SK.NEXTVAL"


def test_groovy_params_pass_the_transport_validator():
    from odi_orchestrator import _PARAM_KEY_RE, _PARAM_VAL_RE, _SECRET_PARAM_KEY_RE

    plan = build_scd2_plan(request(), small_columns())
    params = scd2_builder.groovy_params(plan)
    assert params["odi.scd2.target_model"] == "TGT_DM"
    assert params["odi.scd2.column_types"].startswith("CUSTOMER_SK:NUMBER,CUST_ID:NUMBER_10,")
    assert "CREDIT_LIMIT" not in params["odi.scd2.column_types"]
    for key, value in params.items():
        assert _PARAM_KEY_RE.match(key) and not _SECRET_PARAM_KEY_RE.search(key)
        assert _PARAM_VAL_RE.match(value), key


def test_surrogate_key_collision_is_detected():
    cols = small_columns() + [{"name": "CUSTOMER_SK", "data_type": "NUMBER", "nullable": True}]
    with pytest.raises(Scd2PlanError) as excinfo:
        build_scd2_plan(request(tracked_columns=["CUSTOMER_SK"]), cols)
    assert excinfo.value.code == "surrogate_key_collision"
