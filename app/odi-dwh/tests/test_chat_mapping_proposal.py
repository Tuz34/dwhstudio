"""Synthetic, network-free AI Studio proposal contract tests."""

import pytest

from chat_mapping_proposal import normalize_mapping_request


RAW = {
    "operation": "CREATE",
    "src_table": "HR.ORDERS",
    "mapping_name": "MAP_HR_ORDERS_TO_ODS",
    "tgt_table": "ODS_HR_ORDERS",
}


@pytest.mark.parametrize(("wording", "load_type"), [
    ("insert/update on ORDER_ID", "insert_update"),
    ("upsert on ORDER_ID", "insert_update"),
    ("merge on ORDER_ID", "insert_update"),
    ("ekle/güncelle, anahtar ORDER_ID", "insert_update"),
    ("truncate insert", "truncate_load"),
    ("full reload", "truncate_load"),
    ("tam yenile", "truncate_load"),
    ("Type 1", "type1"),
    ("Tip 1", "type1"),
    ("Type 2 history", "type2"),
    ("SCD2", "type2"),
    ("tarihçeli", "type2"),
])
def test_explicit_load_types_in_english_and_turkish(wording, load_type):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS layer as {wording}", RAW)
    assert result["load_type"] == load_type


@pytest.mark.parametrize(("wording", "keys"), [
    ("key ID", ["ID"]),
    ("upsert on CUSTOMER_ID", ["CUSTOMER_ID"]),
    ("primary key CUSTOMER_ID, REGION_ID", ["CUSTOMER_ID", "REGION_ID"]),
    ("anahtar müşteri_id ve region_id", ["MUSTERI_ID", "REGION_ID"]),
    ("CUSTOMER_ID anahtarı", ["CUSTOMER_ID"]),
])
def test_only_named_keys_are_carried(wording, keys):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS layer, {wording}", RAW)
    assert result["key_columns"] == keys


def test_unstated_options_are_absent_even_when_llm_invents_them():
    raw = {**RAW, "load_type": "type2", "key_columns": ["ORDER_ID"]}
    result = normalize_mapping_request("Model HR.ORDERS into ODS layer", raw)
    assert "load_type" not in result
    assert "key_columns" not in result


def test_user_text_overrides_garbled_llm_options():
    raw = {**RAW, "load_type": "DROP TABLE", "key_columns": ["ID;DROP", "BAD COLUMN"]}
    result = normalize_mapping_request("Model HR.ORDERS into ODS as merge on ORDER_ID", raw)
    assert result["load_type"] == "insert_update"
    assert result["key_columns"] == ["ORDER_ID"]


def test_unsafe_identifiers_are_not_salvaged_from_injection_like_tokens():
    raw = {"operation": "CREATE", "src_table": "HR.ORDERS;DROP", "tgt_table": "X;DROP"}
    result = normalize_mapping_request("Model HR.ORDERS;DROP into ODS, key ID;DROP", raw)
    assert "source_schema" not in result
    assert "source_table" not in result
    assert "target_table" not in result
    assert "mapping_name" not in result
    assert "key_columns" not in result


@pytest.mark.parametrize(("layer_phrase", "layer", "table", "name"), [
    ("ODS katmanına", "ODS", "ODS_HR_ORDERS", "MAP_HR_ORDERS_TO_ODS"),
    ("DWH layer", "DWH", "HR_ORDERS", "MAP_HR_ORDERS_TO_DWH"),
    ("EDW layer", "EDW", "EDW_HR_ORDERS", "MAP_HR_ORDERS_TO_EDW"),
])
def test_layer_selects_existing_target_schema_and_naming(layer_phrase, layer, table, name):
    result = normalize_mapping_request(f"Model HR.ORDERS into {layer_phrase}", RAW)
    assert result["target_layer"] == layer
    assert result["target_schema"] == "DWH"
    assert result["target_table"] == table
    assert result["mapping_name"] == name
    assert result["tgt_table"] == table


def test_explicit_target_table_is_preserved_over_layer_default():
    result = normalize_mapping_request(
        "Model HR.ORDERS into EDW, target table DWH.CORE_ORDERS", RAW
    )
    assert result["target_schema"] == "DWH"
    assert result["target_table"] == "CORE_ORDERS"
    assert result["mapping_name"] == "MAP_HR_ORDERS_TO_EDW"


def test_explicit_unqualified_target_after_into_is_preserved():
    result = normalize_mapping_request(
        "Model HR.ORDERS into CORE_ORDERS in EDW layer", RAW
    )
    assert result["source_table"] == "ORDERS"
    assert result["target_table"] == "CORE_ORDERS"
    assert result["mapping_name"] == "MAP_HR_ORDERS_TO_EDW"


def test_ordinary_layer_phrase_is_not_mistaken_for_a_target_table():
    result = normalize_mapping_request("Model HR.ORDERS into the ODS layer", RAW)
    assert result["target_table"] == "ODS_HR_ORDERS"


def test_dwh_studio_name_does_not_override_ods_destination():
    result = normalize_mapping_request(
        "Model HR.ORDERS into ODS katmanına in DWH Studio", RAW
    )
    assert result["target_layer"] == "ODS"


def test_source_schema_and_table_can_be_stated_separately_without_llm_hints():
    result = normalize_mapping_request(
        "Model source schema HR table ORDERS into ODS",
        {"operation": "CREATE"},
    )
    assert result["source_schema"] == "HR"
    assert result["source_table"] == "ORDERS"
    assert result["mapping_name"] == "MAP_HR_ORDERS_TO_ODS"


def test_user_fields_survive_missing_llm_fields():
    result = normalize_mapping_request(
        "Model HR.ORDERS into DM as truncate insert, key ORDER_ID",
        {"operation": "CREATE"},
    )
    assert result["target_layer"] == "DM"
    assert result["target_schema"] == "DM"
    assert "target_table" not in result
    assert "mapping_name" not in result
    assert result["load_type"] == "truncate_load"
    assert result["key_columns"] == ["ORDER_ID"]
    assert result["warnings"] == ["Datamart targets are supported only for Type 2 history."]


def test_datamart_keeps_explicit_target_without_inventing_generic_name():
    result = normalize_mapping_request(
        "Model HR.ORDERS into datamart, target table DM.DIM_ORDERS as Type 2",
        {"operation": "CREATE"},
    )
    assert result["target_layer"] == "DM"
    assert result["target_schema"] == "DM"
    assert result["target_table"] == "DIM_ORDERS"
    assert "mapping_name" not in result
    assert result["load_type"] == "type2"


def test_no_recognized_layer_keeps_validated_llm_target_as_suggestion():
    result = normalize_mapping_request("Model HR.ORDERS into silver layer", RAW)
    assert "mapping_name" not in result
    assert "target_layer" not in result
    assert result["target_table"] == "ODS_HR_ORDERS"
    assert "target_table" not in result["stated_fields"]


def test_llm_identifier_hints_are_checked_before_use():
    result = normalize_mapping_request(
        "Model table ORDERS into ODS",
        {"operation": "CREATE", "src_schema": "HR;DROP", "tgt_table": "X;DROP"},
    )
    assert result["source_table"] == "ORDERS"
    assert "source_schema" not in result
    assert "target_table" not in result


def test_update_keeps_validated_existing_change_fields():
    result = normalize_mapping_request(
        "Update the mapping target table",
        {"operation": " update ", "mapping_name": "MAP_HR_ORDERS_TO_ODS", "changes": {
            "tgt_table": "ODS_HR_ORDERS_V2", "description": "Use the second target",
        }},
    )
    assert result["changes"] == {
        "tgt_table": "ODS_HR_ORDERS_V2", "description": "Use the second target",
    }
    bad = normalize_mapping_request(
        "Update mapping", {"operation": "UPDATE", "mapping_name": "MAP_HR_ORDERS_TO_ODS",
                           "changes": {"tgt_table": "X;DROP"}},
    )
    assert "changes" not in bad


@pytest.mark.parametrize("wording", [
    "do not truncate", "don't truncate", "never truncate", "not a full reload",
    "without history", "do not merge", "not type 2", "no SCD2",
    "truncate yapma", "truncate etme", "tip 2 değil", "tarihçeli olmasın",
    "truncate hariç", "type 2 tarihçesiz", "not type 2 history",
    "type 2 history değil",
])
def test_negated_load_phrase_never_prefills_a_type(wording):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert "load_type" not in result


@pytest.mark.parametrize(("wording", "expected"), [
    ("do not truncate, use merge", "insert_update"),
    ("not type 2, truncate insert", "truncate_load"),
    ("do not truncate and do not merge", None),
    ("type 1 and type 2", None),
])
def test_only_one_positive_load_type_survives_negation(wording, expected):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert result.get("load_type") == expected


@pytest.mark.parametrize("table", ["TRUNCATE", "MERGE", "UPSERT", "HISTORY", "SCD2"])
def test_qualified_table_name_is_not_a_load_instruction(table):
    result = normalize_mapping_request(f"Model HR.{table} into ODS", {})
    assert result["source_table"] == table
    assert "load_type" not in result
    assert "key_columns" not in result


@pytest.mark.parametrize(("message", "owner", "table"), [
    ("Model the ORDERS table into the ODS layer", None, "ORDERS"),
    ("Load from the HR.ORDERS table into ODS", "HR", "ORDERS"),
    ("ORDERS tablosunu ODS katmanına modelle", None, "ORDERS"),
    ("HR.ORDERS tablosundan ODS katmanına aktar", "HR", "ORDERS"),
])
def test_source_extraction_handles_articles_and_turkish_suffixes(message, owner, table):
    result = normalize_mapping_request(message, {"src_table": "SH.CUSTOMERS"})
    assert result["source_table"] == table
    assert result.get("source_schema") == owner
    assert result["src_table"] == table


def test_source_schema_table_pair_never_mix_disagreeing_origins():
    message = "Model the ORDERS table into ODS"
    same = normalize_mapping_request(message, {"src_table": "HR.ORDERS"})
    other = normalize_mapping_request(message, {"src_table": "SH.CUSTOMERS"})
    stated = normalize_mapping_request("Model HR.ORDERS into ODS", {"src_table": "SH.CUSTOMERS"})
    assert (same["source_schema"], same["source_table"]) == ("HR", "ORDERS")
    assert "source_schema" not in same["stated_fields"]
    assert "source_schema" not in other
    assert other["source_table"] == "ORDERS"
    assert "target_table" not in other
    assert (stated["source_schema"], stated["source_table"]) == ("HR", "ORDERS")


@pytest.mark.parametrize("wording", [
    "column KEY and STATUS", "upsert on Monday", "key ORDER ID",
    "on CUSTOMER_ID", "key A, BAD;DROP", "key today", "upsert on bugün",
])
def test_ambiguous_or_invalid_key_tail_is_absent(wording):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert "key_columns" not in result


@pytest.mark.parametrize(("wording", "expected"), [
    ("upsert on the ORDER_ID", ["ORDER_ID"]),
    ("key A, B", ["A", "B"]),
    ("key A and B", ["A", "B"]),
    ("anahtar MUSTERI_ID", ["MUSTERI_ID"]),
])
def test_explicit_valid_key_list_survives(wording, expected):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert result["key_columns"] == expected


def test_datamart_warning_is_fixed_and_only_type2_avoids_it():
    raw = {"warnings": ["untrusted warning"]}
    truncate = normalize_mapping_request("Model HR.ORDERS into DM as truncate insert", raw)
    unknown = normalize_mapping_request("Model HR.ORDERS into datamart", raw)
    type2 = normalize_mapping_request("Model HR.ORDERS into data mart as Type 2", raw)
    warning = "Datamart targets are supported only for Type 2 history."
    assert truncate["warnings"] == [warning]
    assert unknown["warnings"] == [warning]
    assert "warnings" not in type2


def test_no_layer_llm_hints_are_validated_and_marked_as_suggestions():
    raw = {"src_table": "HR.EMPLOYEES", "tgt_owner": "DWH",
           "tgt_table": "ODS_HR_EMPLOYEES", "load_type": "type2",
           "key_columns": ["EMPLOYEE_ID"], "stated_fields": ["source_table"]}
    result = normalize_mapping_request("hello there", raw)
    assert (result["source_schema"], result["source_table"]) == ("HR", "EMPLOYEES")
    assert (result["target_schema"], result["target_table"]) == ("DWH", "ODS_HR_EMPLOYEES")
    assert result["src_table"] == "EMPLOYEES"
    assert result["stated_fields"] == []
    assert "load_type" not in result and "key_columns" not in result


def test_type2_long_generated_name_is_omitted_but_explicit_name_is_preserved():
    long_name = "MAP_HR_ORDER_LINE_ITEM_HISTORY_TO_EDW"
    assert len(long_name) == 37
    generated = normalize_mapping_request(
        "Model HR.ORDER_LINE_ITEM_HISTORY into EDW as Type 2", {})
    explicit = normalize_mapping_request(
        f"Model HR.ORDERS into EDW as Type 2, mapping name {long_name}", {})
    assert "mapping_name" not in generated
    assert "warnings" not in generated
    assert explicit["mapping_name"] == long_name
    assert explicit["warnings"] == [
        "Mapping name is longer than 31 characters; Type 2 requires a shorter name."]


def test_type2_proposal_fixture_has_unqualified_wizard_source_fields():
    result = normalize_mapping_request(
        "Model HR.ORDERS into EDW as Type 2, key ORDER_ID", {})
    assert {key: result[key] for key in (
        "src_model", "src_schema", "src_table", "tgt_model", "tgt_owner",
        "tgt_table", "mapping_name", "load_type", "key_columns",
    )} == {
        "src_model": "ORACLE_SOURCE", "src_schema": "HR", "src_table": "ORDERS",
        "tgt_model": "ORACLE_DWH", "tgt_owner": "DWH", "tgt_table": "EDW_HR_ORDERS",
        "mapping_name": "MAP_HR_ORDERS_TO_EDW", "load_type": "type2",
        "key_columns": ["ORDER_ID"],
    }


def test_api_ask_returns_normalized_proposal_without_raw_llm_text(app_client, monkeypatch):
    import app as app_module

    for name in (
        "_is_llm_status_question", "_is_truncate_mapping_intent",
        "_is_remembered_unmapped_flow_mapping_request", "_is_unmapped_odi_flow_question",
        "_is_modeled_tables_question", "_is_table_data_request",
    ):
        monkeypatch.setattr(app_module, name, lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app_module, "ask_ai", lambda *_args, **_kwargs: (
        {"mapping_request": {**RAW, "load_type": "garbage", "key_columns": ["BAD;SQL"]}},
        "untrusted model output",
    ))

    response = app_client.post(
        "/api/ask", json={"message": "Model HR.ORDERS into EDW as SCD2 on ORDER_ID"}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["mapping_request"]["target_table"] == "EDW_HR_ORDERS"
    assert body["mapping_request"]["load_type"] == "type2"
    assert body["mapping_request"]["key_columns"] == ["ORDER_ID"]
    assert body["raw"] == ""
