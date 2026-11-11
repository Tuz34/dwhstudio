"""Offline contracts for ODI-81's Datamart placement decision."""

import pytest

from chat_mapping_proposal import normalize_mapping_request


_PLACEMENT_WARNING = (
    "Datamart objects are created in DWH with a DM_ prefix in this installation."
)


@pytest.mark.parametrize(("sentence", "expected"), [
    (
        "Model HR.ORDERS into DATAMART as Type 2, key ORDER_ID",
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DWH", "tgt_owner": "DWH",
            "target_table": "DM_HR_ORDERS", "tgt_table": "DM_HR_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "key_columns": ["ORDER_ID"],
            "stated_fields": ["source_schema", "source_table", "target_layer", "load_type", "key_columns"],
        },
    ),
    (
        "HR.ORDERS tablosunu datamart katmanına tip 2 olarak modelle, anahtar ORDER_ID",
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DWH", "tgt_owner": "DWH",
            "target_table": "DM_HR_ORDERS", "tgt_table": "DM_HR_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "key_columns": ["ORDER_ID"],
            "stated_fields": ["source_schema", "source_table", "target_layer", "load_type", "key_columns"],
        },
    ),
    (
        'Model "HR"."ORDERS" into data mart as Type 2',
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DWH", "tgt_owner": "DWH",
            "target_table": "DM_HR_ORDERS", "tgt_table": "DM_HR_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "stated_fields": ["source_schema", "source_table", "target_layer", "load_type"],
        },
    ),
    (
        "Model HR.ORDERS into datamart as Type 2, target table DIM_ORDERS",
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DWH", "tgt_owner": "DWH",
            "target_table": "DIM_ORDERS", "tgt_table": "DIM_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "warnings": [_PLACEMENT_WARNING],
            "stated_fields": ["source_schema", "source_table", "target_layer", "target_table", "load_type"],
        },
    ),
    (
        "Model HR.ORDERS into datamart as Type 2, target table DM.DM_DIM_ORDERS",
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DM", "tgt_owner": "DM",
            "target_table": "DM_DIM_ORDERS", "tgt_table": "DM_DIM_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "warnings": [_PLACEMENT_WARNING],
            "stated_fields": ["source_schema", "source_table", "target_layer", "target_schema", "target_table", "load_type"],
        },
    ),
    (
        "Model HR.ORDERS into datamart as Type 2, target schema DM",
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DM", "tgt_owner": "DM",
            "target_table": "DM_HR_ORDERS", "tgt_table": "DM_HR_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "warnings": [_PLACEMENT_WARNING],
            "stated_fields": ["source_schema", "source_table", "target_layer", "target_schema", "load_type"],
        },
    ),
    (
        'Model HR.ORDERS into datamart as Type 2, target table "DM"."DIM_ORDERS"',
        {
            "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
            "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
            "target_layer": "DM", "target_schema": "DM", "tgt_owner": "DM",
            "target_table": "DIM_ORDERS", "tgt_table": "DIM_ORDERS",
            "mapping_name": "MAP_HR_ORDERS_TO_DM", "load_type": "type2",
            "warnings": [_PLACEMENT_WARNING],
            "stated_fields": ["source_schema", "source_table", "target_layer", "target_schema", "target_table", "load_type"],
        },
    ),
])
def test_datamart_proposal_is_exact(sentence, expected):
    assert normalize_mapping_request(sentence, {"operation": "CREATE"}) == expected


@pytest.mark.parametrize(("layer", "table", "mapping", "model"), [
    ("ODS", "ODS_HR_ORDERS", "MAP_HR_ORDERS_TO_ODS", "ORACLE_ODS"),
    ("EDW", "EDW_HR_ORDERS", "MAP_HR_ORDERS_TO_EDW", "ORACLE_DWH"),
    ("DWH", "HR_ORDERS", "MAP_HR_ORDERS_TO_DWH", "ORACLE_DWH"),
])
def test_other_layers_match_base_golden_proposal(layer, table, mapping, model):
    result = normalize_mapping_request(
        f"Model HR.ORDERS into {layer} as Type 2, key ORDER_ID", {"operation": "CREATE"}
    )
    assert result == {
        "operation": "CREATE", "source_schema": "HR", "src_schema": "HR",
        "source_table": "ORDERS", "src_table": "ORDERS", "src_model": "ORACLE_SOURCE",
        "target_layer": layer, "tgt_model": model, "target_schema": "DWH", "tgt_owner": "DWH",
        "target_table": table, "tgt_table": table, "mapping_name": mapping,
        "load_type": "type2", "key_columns": ["ORDER_ID"],
        "stated_fields": ["source_schema", "source_table", "target_layer", "load_type", "key_columns"],
    }


def test_datamart_non_type2_keeps_classic_restriction():
    result = normalize_mapping_request("Model HR.ORDERS into DM as truncate insert", {})
    assert result["target_layer"] == "DM"
    assert result["target_schema"] == "DWH"
    assert result["target_table"] == "DM_HR_ORDERS"
    assert result["warnings"] == ["Datamart targets are supported only for Type 2 history."]
    assert "tgt_model" not in result


@pytest.mark.parametrize("layer_phrase", ["DM'e", "datamart'a", "DM layer", "DATA MART layer"])
def test_datamart_layer_spellings_share_the_new_default(layer_phrase):
    result = normalize_mapping_request(f"Model HR.ORDERS {layer_phrase} as Type 2", {})
    assert (result["target_layer"], result["target_schema"], result["target_table"]) == (
        "DM", "DWH", "DM_HR_ORDERS"
    )


def test_stated_datamart_schema_and_table_are_both_preserved():
    result = normalize_mapping_request(
        "Model HR.ORDERS into datamart as Type 2, target schema DM, target table DM_ORDERS", {}
    )
    assert (result["target_schema"], result["target_table"]) == ("DM", "DM_ORDERS")
    assert result["warnings"] == [_PLACEMENT_WARNING]
    assert result["stated_fields"] == [
        "source_schema", "source_table", "target_layer", "target_schema", "target_table", "load_type"
    ]


def test_explicit_dm_prefixed_table_in_dwh_needs_no_placement_warning():
    result = normalize_mapping_request(
        "Model HR.ORDERS into datamart as Type 2, target table DWH.DM_CUSTOMERS", {}
    )
    assert (result["target_schema"], result["target_table"]) == ("DWH", "DM_CUSTOMERS")
    assert "warnings" not in result


def test_datamart_long_generated_names_follow_existing_limits():
    source = "ORDER_LINE_ITEM_HISTORY_LONG"
    result = normalize_mapping_request(f"Model HR.{source} into DM as Type 2", {})
    assert result["target_table"] == f"DM_HR_{source}"
    assert len(result["target_table"]) > 30
    assert "mapping_name" not in result  # Type 2 omits generated names above 31.
    assert "warnings" not in result

    beyond_identifier_limit = "A" * 125
    result = normalize_mapping_request(f"Model HR.{beyond_identifier_limit} into DM as Type 2", {})
    assert result["source_table"] == beyond_identifier_limit
    assert "target_table" not in result  # Generated names still pass the 128-character identifier gate.
    assert "mapping_name" not in result


def test_datamart_source_model_rule_stays_owner_based():
    result = normalize_mapping_request("Model DWH.ORDERS into DM as Type 2", {})
    assert result["src_model"] == "ORACLE_DWH"
    stated = normalize_mapping_request(
        "Model HR.ORDERS source model ORACLE_ODS into DM as Type 2", {}
    )
    assert stated["src_model"] == "ORACLE_ODS"
    assert "source_model" in stated["stated_fields"]
