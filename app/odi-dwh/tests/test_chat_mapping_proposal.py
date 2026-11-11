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
    ("as upsert on CUSTOMER_ID", ["CUSTOMER_ID"]),
    ("primary key CUSTOMER_ID, REGION_ID", ["CUSTOMER_ID", "REGION_ID"]),
    ("anahtar müşteri_id ve region_id", ["MUSTERI_ID", "REGION_ID"]),
    ("anahtar: CUSTOMER_ID", ["CUSTOMER_ID"]),
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
    assert result["src_model"] == "ORACLE_SOURCE"
    assert result["tgt_model"] == {"ODS": "ORACLE_ODS", "DWH": "ORACLE_DWH", "EDW": "ORACLE_DWH"}[layer]


def test_explicit_target_table_is_preserved_over_layer_default():
    result = normalize_mapping_request(
        "Model HR.ORDERS into EDW, target table DWH.CORE_ORDERS", RAW
    )
    assert result["target_schema"] == "DWH"
    assert result["target_table"] == "CORE_ORDERS"
    assert result["mapping_name"] == "MAP_HR_ORDERS_TO_EDW"


def test_explicit_unqualified_target_after_into_table_is_preserved():
    result = normalize_mapping_request(
        "Model HR.ORDERS into table CORE_ORDERS in the EDW layer", RAW
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
    assert result["src_model"] == "ORACLE_SOURCE"
    assert "tgt_model" not in result


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


@pytest.mark.parametrize("wording", [
    "do not truncate, use merge",
    "not type 2, truncate insert",
    "do not truncate and do not merge",
    "as type 1 and using type 2",
])
def test_any_hedge_or_two_load_constructions_leave_type_absent(wording):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert "load_type" not in result


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
    "column KEY and STATUS", "as upsert on Monday", "key ORDER ID",
    "on CUSTOMER_ID", "key A, BAD;DROP", "key today", "as upsert on bugün",
])
def test_ambiguous_or_invalid_key_tail_is_absent(wording):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert "key_columns" not in result


@pytest.mark.parametrize(("wording", "expected"), [
    ("as upsert on the ORDER_ID", ["ORDER_ID"]),
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


OWNER_SENTENCES = {
    "a": (
        [
            "DWH.ODI81N_SRC_F1 tablosunu ODS katmanına insert update olarak modelle, anahtar ID",
            "anahtar ID, insert update olarak ODS katmanına DWH.ODI81N_SRC_F1 tablosunu modelle",
        ],
        {"source_schema": "DWH", "source_table": "ODI81N_SRC_F1", "target_layer": "ODS",
         "load_type": "insert_update", "key_columns": ["ID"], "target_schema": "DWH",
         "target_table": "ODS_DWH_ODI81N_SRC_F1",
         "mapping_name": "MAP_DWH_ODI81N_SRC_F1_TO_ODS", "warnings": None},
    ),
    "b": (
        [
            "Model DWH.ODI81N_SRC_F2 into ODS as truncate insert",
            "Into ODS as truncate insert, model DWH.ODI81N_SRC_F2",
        ],
        {"source_schema": "DWH", "source_table": "ODI81N_SRC_F2", "target_layer": "ODS",
         "load_type": "truncate_load", "key_columns": None, "target_schema": "DWH",
         "target_table": "ODS_DWH_ODI81N_SRC_F2",
         "mapping_name": "MAP_DWH_ODI81N_SRC_F2_TO_ODS", "warnings": None},
    ),
    "c": (
        [
            "DWH.ODS_ODI81N_F4 tablosunu EDW katmanına tip 2 olarak modelle, "
            "anahtar ID, hedef tablo DWH.DIM_ODI81N_F4",
            "hedef tablo DWH.DIM_ODI81N_F4, DWH.ODS_ODI81N_F4 tablosunu "
            "EDW katmanına tip 2 olarak modelle, anahtar ID",
            "anahtar ID, hedef tablo DWH.DIM_ODI81N_F4, EDW katmanına tip 2 olarak "
            "DWH.ODS_ODI81N_F4 tablosunu modelle",
            "tip 2 olarak EDW katmanına DWH.ODS_ODI81N_F4 tablosunu modelle, "
            "hedef tablo DWH.DIM_ODI81N_F4, anahtar ID",
        ],
        {"source_schema": "DWH", "source_table": "ODS_ODI81N_F4", "target_layer": "EDW",
         "load_type": "type2", "key_columns": ["ID"], "target_schema": "DWH",
         "target_table": "DIM_ODI81N_F4",
         "mapping_name": "MAP_DWH_ODS_ODI81N_F4_TO_EDW", "warnings": None},
    ),
    "d": (
        [
            "Model DWH.ODI81N_SRC_F3 into the EDW layer as type 1 with key ID, "
            "target table DWH.DWH_ODI81N_F3",
            "target table DWH.DWH_ODI81N_F3, key ID, as type 1, "
            "model DWH.ODI81N_SRC_F3 into the EDW layer",
        ],
        {"source_schema": "DWH", "source_table": "ODI81N_SRC_F3", "target_layer": "EDW",
         "load_type": "type1", "key_columns": ["ID"], "target_schema": "DWH",
         "target_table": "DWH_ODI81N_F3",
         "mapping_name": "MAP_DWH_ODI81N_SRC_F3_TO_EDW", "warnings": None},
    ),
    "e": (
        [
            "DWH.DWH_ODI81N_F5 tablosunu datamart katmanına tarihçeli olarak modelle, "
            "anahtar ID, hedef tablo DM.DIM_ODI81N_F5",
            "hedef tablo DM.DIM_ODI81N_F5, anahtar ID, tarihçeli olarak "
            "datamart katmanına DWH.DWH_ODI81N_F5 tablosunu modelle",
        ],
        {"source_schema": "DWH", "source_table": "DWH_ODI81N_F5", "target_layer": "DM",
         "load_type": "type2", "key_columns": ["ID"], "target_schema": "DM",
         "target_table": "DIM_ODI81N_F5", "mapping_name": None, "warnings": None},
    ),
}


@pytest.mark.parametrize(("label", "sentence", "expected"), [
    (f"{label}-{index}", sentence, expected)
    for label, (sentences, expected) in OWNER_SENTENCES.items()
    for index, sentence in enumerate(sentences)
])
def test_owner_sentences_and_permutations_have_complete_proposals(label, sentence, expected):
    result = normalize_mapping_request(sentence, {"operation": "CREATE"})
    assert {field: result.get(field) for field in expected} == expected, label
    assert result["src_schema"] == expected["source_schema"]
    assert result["src_table"] == expected["source_table"]
    assert result.get("tgt_owner") == expected["target_schema"]
    assert result.get("tgt_table") == expected["target_table"]
    stated = ["source_schema", "source_table", "target_layer"]
    if label.startswith(("c", "d", "e")):
        stated.extend(["target_schema", "target_table"])
    stated.append("load_type")
    if expected["key_columns"]:
        stated.append("key_columns")
    assert result["stated_fields"] == stated
    assert result["src_model"] == "ORACLE_DWH"
    if expected["target_layer"] == "ODS":
        assert result["tgt_model"] == "ORACLE_ODS"
    elif expected["target_layer"] == "DM":
        assert "tgt_model" not in result
    else:
        assert result["tgt_model"] == "ORACLE_DWH"


def test_dwh_owner_to_ods_matches_classic_acceptance_models():
    result = normalize_mapping_request(
        "DWH.ODI81N_SRC_F1 tablosunu ODS katmanına insert update olarak modelle, anahtar ID",
        {"operation": "CREATE"},
    )
    assert result["src_schema"] == "DWH"
    assert result["src_table"] == "ODI81N_SRC_F1"
    assert result["target_layer"] == "ODS"
    assert result["src_model"] == "ORACLE_DWH"
    assert result["tgt_model"] == "ORACLE_ODS"


@pytest.mark.parametrize("owner", ["ODS", "AUDIT_USER", "DWH_FLAT"])
def test_other_internal_owners_use_the_classic_internal_source_model(owner):
    result = normalize_mapping_request(f"Model {owner}.ORDERS into ODS as merge, key ID", {})
    assert result["src_model"] == "ORACLE_DWH"
    assert result["tgt_model"] == "ORACLE_ODS"


def test_external_owner_keeps_the_external_source_model():
    result = normalize_mapping_request("Model SH.CUSTOMERS into ODS as merge, key ID", {})
    assert result["src_model"] == "ORACLE_SOURCE"
    assert result["tgt_model"] == "ORACLE_ODS"


@pytest.mark.parametrize(("wording", "expected"), [
    ("as merge", "insert_update"), ("using upsert", "insert_update"),
    ("with load insert/update", "insert_update"),
    ("load type full reload", "truncate_load"),
    ("as truncate insert", "truncate_load"), ("with mode type 1", "type1"),
    ("using SCD2", "type2"), ("insert update olarak", "insert_update"),
    ("truncate insert ile", "truncate_load"), ("tip 1 şeklinde", "type1"),
    ("tarihçeli olarak", "type2"), ("yükleme tipi tip 2", "type2"),
])
def test_load_type_requires_an_assignment_construction(wording, expected):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert result["load_type"] == expected


@pytest.mark.parametrize("wording", [
    "truncate", "merge", "upsert", "type 1", "type 2", "history",
    "SCD2", "insert update", "ekle güncelle", "tam yenile",
    "Model HISTORY into ODS", "Model TRUNCATE into ODS",
])
def test_bare_load_words_never_prefill(wording):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert "load_type" not in result


@pytest.mark.parametrize("message", [
    "merge the customer name", "type 2 with the same key", "truncate schema",
    "Model ORDERS into ODS", "merge HR.ORDERS into ODS on ORDER_ID",
    "do not want to truncate HR.ORDERS into ODS",
    "rather than truncate, merge the customer name",
])
def test_reviewed_non_assignment_phrases_have_no_load_or_keys(message):
    result = normalize_mapping_request(message, {})
    assert "load_type" not in result
    assert "key_columns" not in result


@pytest.mark.parametrize("hedge", [
    "not", "no", "never", "don't", "dont", "don’t", "without", "avoid",
    "rather than", "instead of", "except", "unless", "only if", "if", "should",
    "?", "değil", "olmasın", "hariç", "yerine", "eğer", "istemiyorum",
    "tarihçesiz",
    "yapma", "etme", "tutmasın", "kullanmadan", "etmeden", "yapmayın", "tutmaz",
])
def test_any_hedge_anywhere_suppresses_assigned_load_type(hedge):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS as truncate insert, {hedge}", {})
    assert "load_type" not in result


@pytest.mark.parametrize("message", [
    "Model HR.ORDERS into ODS as merge, key ID?",
    "Model HR.ORDERS into ODS as merge, key NOT",
    "Model HR.ORDERS into ODS as merge, target table BAD?",
])
def test_invalid_identifier_does_not_hide_a_hedge(message):
    result = normalize_mapping_request(message, {})
    assert "load_type" not in result


def test_valid_identifier_named_not_is_consumed_before_hedge_detection():
    result = normalize_mapping_request("Model HR.ORDERS into ODS as merge, target table NOT", {})
    assert result["target_table"] == "NOT"
    assert result["load_type"] == "insert_update"


@pytest.mark.parametrize(("message", "layer", "target", "owner"), [
    ("Model HR.ORDERS, target table ODS", None, "ODS", None),
    ("Model HR.ORDERS into DWH, target table EDW", "DWH", "EDW", "DWH"),
    ("Model HR.ORDERS, hedef tablo DM", None, "DM", None),
    ("Model HR.ORDERS into ODS, target table MERGE", "ODS", "MERGE", "DWH"),
    ("Model HR.ORDERS into table CORE_ORDERS in the EDW layer", "EDW", "CORE_ORDERS", "DWH"),
    ("Model HR.ORDERS into CORE_ORDERS in the EDW layer", "EDW", "EDW_HR_ORDERS", "DWH"),
    ("I want to truncate HR.ORDERS into ODS", "ODS", "ODS_HR_ORDERS", "DWH"),
    ("I want to merge HR.ORDERS into ODS on ORDER_ID", "ODS", "ODS_HR_ORDERS", "DWH"),
])
def test_target_requires_marker_and_layer_ignores_target_identifiers(message, layer, target, owner):
    result = normalize_mapping_request(message, {})
    assert result.get("target_layer") == layer
    assert result.get("target_table") == target
    assert result.get("target_schema") == owner
    assert "load_type" not in result
    assert "key_columns" not in result


@pytest.mark.parametrize(("phrase", "layer"), [
    ("into ODS", "ODS"), ("to DWH", "DWH"), ("in the EDW layer", "EDW"),
    ("DM layer", "DM"), ("datamart katmanına", "DM"),
    ("ODS katmanında", "ODS"), ("layer EDW", "EDW"),
    ("katman DWH", "DWH"),
])
def test_layer_requires_an_explicit_construction(phrase, layer):
    result = normalize_mapping_request(f"Model HR.ORDERS {phrase}", {})
    assert result["target_layer"] == layer


@pytest.mark.parametrize("message", [
    "Model HR.ORDERS, target table ODS layer",
    "Model HR.ORDERS, hedef tablo DM katmanı",
    "Model HR.ORDERS, target table MERGE olarak",
    "Model table HISTORY olarak",
    "Model HR.ORDERS, key MERGE olarak",
])
def test_identifier_tokens_do_not_become_layer_or_load_constructions(message):
    result = normalize_mapping_request(message, {})
    assert "target_layer" not in result
    assert "load_type" not in result


@pytest.mark.parametrize(("message", "source", "target"), [
    ("hedef tablo DWH.DIM_ORDERS, HR.ORDERS tablosunu EDW katmanına modelle",
     ("HR", "ORDERS"), ("DWH", "DIM_ORDERS")),
    ("target table DWH.DIM_ORDERS from HR.ORDERS",
     ("HR", "ORDERS"), ("DWH", "DIM_ORDERS")),
    ("Model ORDERS into ODS", (None, None), ("DWH", None)),
    ("Model HISTORY into ODS", (None, None), ("DWH", None)),
    ("Model HR.ORDERS into ODS, target table HR.ORDERS",
     ("HR", "ORDERS"), (None, None)),
])
def test_source_and_target_positive_grammar(message, source, target):
    result = normalize_mapping_request(message, {})
    assert (result.get("source_schema"), result.get("source_table")) == source
    assert (result.get("target_schema"), result.get("target_table")) == target


@pytest.mark.parametrize(("owner", "table"), [
    ("ODS", "CUSTOMERS"), ("DM", "SALES"), ("EDW", "FACT_SALES"),
])
def test_layer_word_as_qualified_source_owner_stays_an_identifier(owner, table):
    result = normalize_mapping_request(f"Model {owner}.{table} as merge", {})
    assert (result["source_schema"], result["source_table"]) == (owner, table)
    assert "target_layer" not in result
    assert result["load_type"] == "insert_update"


def test_function_word_in_qualified_source_is_one_identifier_token():
    result = normalize_mapping_request("Model HR.ON into ODS as merge", {})
    assert (result["source_schema"], result["source_table"]) == ("HR", "ON")


@pytest.mark.parametrize(("wording", "keys"), [
    ("keys: A, B and C", ["A", "B", "C"]),
    ("key:ID", ["ID"]),
    ("anahtar: A ve B", ["A", "B"]),
    ("key (A, B)", ["A", "B"]),
    ("key A, A, B", ["A", "B"]),
    ("key ID, hedef tablo DWH.DIM_ORDERS", ["ID"]),
    ("key ID, EDW layer", ["ID"]),
    ("key ID, as type 2", ["ID"]),
    ("as merge on A and B", ["A", "B"]),
    ("as merge on A, B", ["A", "B"]),
    ("as merge on the ORDER_ID", ["ORDER_ID"]),
    ("merge on A and B", None),
    ("key ON", None),
    ("key ORDER ID", None),
    ("key A, BAD;DROP", None),
    ('key "ID"', None),
    ("key A, B, C, D, E, F, G, H, I", None),
])
def test_key_list_ends_at_new_construction_but_rejects_bad_list_tokens(wording, keys):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS, {wording}", {})
    assert result.get("key_columns") == keys


@pytest.mark.parametrize("ending", [
    "as MERGE.LOG", "using TRUNCATE.AUDIT", "with HISTORY.SALES",
    "using SCD2.DIM", "as UPSERT.STG", "type 2 OLARAK.DIM",
    "truncate ILE.LOG",
])
def test_dotted_tokens_cannot_complete_load_constructions(ending):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS {ending}", {})
    assert (result["source_schema"], result["source_table"]) == ("HR", "ORDERS")
    assert result["target_layer"] == "ODS"
    assert result["target_table"] == "ODS_HR_ORDERS"
    assert "load_type" not in result
    assert "key_columns" not in result


@pytest.mark.parametrize(("ending", "load_type", "keys"), [
    ("into ODS.STAGING as type 1 with key ID", "type1", ["ID"]),
    ("to EDW.FACT_SALES as type 1 with key ID", "type1", ["ID"]),
    ("to DM.SALES as type 2 with key ID", "type2", ["ID"]),
    ("into DATAMART.SALES as truncate insert", "truncate_load", None),
    ("in the ODS.CUSTOMERS as merge on ORDER_ID", "insert_update", ["ORDER_ID"]),
    ("layer DM.SALES", None, None),
    ("to DATA MART.SALES", None, None),
])
def test_dotted_tokens_after_layer_markers_do_not_invent_layer_or_target(ending, load_type, keys):
    result = normalize_mapping_request(f"Model HR.ORDERS {ending}", {})
    assert (result["source_schema"], result["source_table"]) == ("HR", "ORDERS")
    assert result.get("target_layer") is None
    assert result.get("target_table") is None
    assert result.get("target_schema") is None
    assert result.get("load_type") == load_type
    assert result.get("key_columns") == keys
    assert "warnings" not in result


@pytest.mark.parametrize(("message", "source", "keys"), [
    ("ref DWH.KEY CUSTOMER_ID", "KEY", None),
    ("ref DWH.ANAHTAR MUSTERI_ID", "ANAHTAR", None),
    ("DWH.PK anahtar ID", "PK", ["ID"]),
])
def test_key_marker_inside_dotted_name_is_inert(message, source, keys):
    result = normalize_mapping_request(message, {})
    assert (result["source_schema"], result["source_table"]) == ("DWH", source)
    assert result.get("key_columns") == keys


def test_dotted_olarak_is_a_source_table_not_a_layer_construction():
    result = normalize_mapping_request(
        "DWH.OLARAK tablosunu ODS katmanına truncate insert olarak modelle", {})
    assert (result["source_schema"], result["source_table"]) == ("DWH", "OLARAK")
    assert result["target_layer"] == "ODS"
    assert result["target_table"] == "ODS_DWH_OLARAK"
    assert result["mapping_name"] == "MAP_DWH_OLARAK_TO_ODS"
    assert result["load_type"] == "truncate_load"


@pytest.mark.parametrize("source_ref", [
    '"HR"."ORDERS"', "'HR'.'ORDERS'", "`HR`.`ORDERS`", "“HR”.“ORDERS”",
])
def test_quoted_parts_of_dotted_reference_stay_one_source_identifier(source_ref):
    result = normalize_mapping_request(f"Model {source_ref} into ODS as merge", {})
    assert (result["source_schema"], result["source_table"]) == ("HR", "ORDERS")
    assert result["target_layer"] == "ODS"
    assert result["load_type"] == "insert_update"


def test_whole_quoted_dotted_name_does_not_supply_a_source():
    result = normalize_mapping_request('Model "HR.ORDERS" into ODS as merge', {})
    assert "source_table" not in result
    assert result["target_layer"] == "ODS"


@pytest.mark.parametrize("example", [
    'Example: "as truncate insert".',
    "Örnek: 'truncate insert olarak'.",
    "Example: `as merge on ID`.",
    "Örnek: “tip 2 olarak”.",
    "Örnek: ‘tarihçeli olarak’.",
    "Example: 'don't use as truncate insert'.",
    "Örnek: ‘don’t use as truncate insert’.",
])
def test_quoted_examples_never_assign_load_or_keys(example):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS. {example}", {})
    assert result["target_layer"] == "ODS"
    assert "load_type" not in result
    assert "key_columns" not in result


@pytest.mark.parametrize("hedge", [
    "değildir", "değilse", "yapmasın", "etmeyiniz", "you can't",
    "I cannot", "shouldn't", "isn't", "won't", "avoided", "shouldnt",
    "istemem", "silmeyin", "yüklemesin", "hayır",
])
def test_inflected_hedges_clear_assigned_load_type(hedge):
    result = normalize_mapping_request(
        f"Model HR.ORDERS into ODS as truncate insert, {hedge}", {})
    assert "load_type" not in result


def test_hedge_in_later_sentence_clears_assigned_load_type():
    result = normalize_mapping_request(
        "Model HR.ORDERS into ODS as truncate insert. Do not run it.", {})
    assert "load_type" not in result


@pytest.mark.parametrize("message", [
    "Model HR.ORDERS into ODS as truncate insert değildir",
    "Model HR.ORDERS into ODS as truncate insert değilse",
    "Model HR.ORDERS into ODS as truncate insert yapmasın",
    "Model HR.ORDERS into ODS as truncate insert etmeyiniz",
    "Model HR.ORDERS into ODS as truncate insert, you can't",
    "I cannot model HR.ORDERS into ODS as truncate insert",
])
def test_reviewer_hedge_sentences_never_assign_load(message):
    assert "load_type" not in normalize_mapping_request(message, {})


GRAMMAR_WORDS = [
    "MERGE", "TRUNCATE", "UPSERT", "INSERT", "UPDATE", "FULL", "RELOAD",
    "HISTORY", "HISTORICAL", "SCD", "SCD1", "SCD2", "TYPE", "TIP",
    "TARIHCELI", "EKLE", "EKLEME", "GUNCELLE", "GUNCELLEME",
    "VARSA", "YOKSA", "TAM", "YUKLEME", "YENILE", "YENILEME",
    "ODS", "DWH", "EDW", "DM", "DATAMART", "DATA", "MART",
    "KEY", "KEYS", "ANAHTAR", "PK", "PRIMARY", "NATURAL",
    "AS", "USING", "WITH", "OLARAK", "ILE", "SEKLINDE", "TARGET",
    "TABLE", "HEDEF", "INTO", "TO", "LAYER", "KATMAN",
    "NOT", "NO", "NEVER", "SHOULD", "AVOID", "IF", "DEGIL", "YAPMASIN",
    "GEREKSIZ",
]


def test_grammar_word_property_fixture_is_nonempty_and_covers_each_category():
    words = set(GRAMMAR_WORDS)
    assert words
    for category in (
        {"INSERT", "UPDATE", "MERGE", "UPSERT", "TRUNCATE", "FULL", "RELOAD",
         "TYPE", "TIP", "SCD2", "TARIHCELI", "EKLEME", "GUNCELLEME"},
        {"ODS", "DWH", "EDW", "DM", "DATAMART", "DATA", "MART"},
        {"KEY", "KEYS", "ANAHTAR", "PK", "PRIMARY", "NATURAL"},
        {"AS", "USING", "WITH", "OLARAK", "ILE", "SEKLINDE", "TARGET", "HEDEF", "INTO", "LAYER"},
        {"NOT", "NO", "NEVER", "SHOULD", "AVOID", "IF", "DEGIL", "YAPMASIN",
         "GEREKSIZ"},
    ):
        assert category <= words


@pytest.mark.parametrize("word", GRAMMAR_WORDS)
def test_grammar_word_inside_dotted_identifier_is_never_a_construction(word):
    messages = [
        f"Model HR.{word} into ODS",
        f"Model {word}.T1 into ODS",
        f"Model HR.ORDERS into ODS, target table DWH.{word}",
    ]
    for message in messages:
        result = normalize_mapping_request(message, {})
        assert result["target_layer"] == "ODS", message
        assert "load_type" not in result, message
        assert "key_columns" not in result, message


@pytest.mark.parametrize(("message", "load_type", "keys", "layer"), [
    ("DWH.ODI81N_SRC_F1 tablosunu ODS katmanına insert/update olarak modelle, anahtar: ID",
     "insert_update", ["ID"], "ODS"),
    ("DWH.ODI81N_SRC_F1 tablosunu ODS katmanına upsert olarak modelle, key ID",
     "insert_update", ["ID"], "ODS"),
    ("DWH.ODI81N_SRC_F2 tablosunu ODS katmanına truncate insert ile modelle",
     "truncate_load", None, "ODS"),
    ("DWH.ODI81N_SRC_F2 tablosunu ODS katmanına full reload olarak modelle",
     "truncate_load", None, "ODS"),
    ("DWH.ODI81N_SRC_F3 tablosunu EDW katmanına tip 1 olarak modelle, anahtar ID",
     "type1", ["ID"], "EDW"),
    ("DWH.ODS_ODI81N_F4 tablosunu EDW katmanına type 2 olarak modelle, anahtar ID ve KOD",
     "type2", ["ID", "KOD"], "EDW"),
    ("DWH.ODS_ODI81N_F4 tablosunu EDW katmanına SCD2 olarak modelle, key ID",
     "type2", ["ID"], "EDW"),
    ("model dwh.odi81n_src_f2 into ods as truncate insert.", "truncate_load", None, "ODS"),
    ("DWH.DWH_ODI81N_F5 tablosunu DM katmanına tarihçeli olarak modelle, anahtar ID",
     "type2", ["ID"], "DM"),
    ("DWH.DWH_ODI81N_F5 tablosunu data mart katmanına tarihçeli olarak modelle, anahtar ID",
     "type2", ["ID"], "DM"),
])
def test_owner_wording_variants_keep_expected_prefills(message, load_type, keys, layer):
    result = normalize_mapping_request(message, {})
    assert result["target_layer"] == layer
    assert result["load_type"] == load_type
    assert result.get("key_columns") == keys
    assert result["source_schema"] == "DWH"


@pytest.mark.parametrize("source_ref", [
    "DWH.ODI81N_SRC_F1'i", "DWH.ODI81N_SRC_F1’i",
    "DWH.ODI81N_SRC_F1’den",
])
@pytest.mark.parametrize("with_marker", [False, True])
def test_turkish_case_suffix_keeps_qualified_source(source_ref, with_marker):
    marker = " tablosunu" if with_marker else ""
    result = normalize_mapping_request(
        f"{source_ref}{marker} ODS katmanına insert update olarak modelle, anahtar ID", {})
    assert (result["source_schema"], result["source_table"]) == ("DWH", "ODI81N_SRC_F1")
    assert result["target_layer"] == "ODS"
    assert result["target_table"] == "ODS_DWH_ODI81N_SRC_F1"
    assert result["mapping_name"] == "MAP_DWH_ODI81N_SRC_F1_TO_ODS"
    assert result["load_type"] == "insert_update"
    assert result["key_columns"] == ["ID"]


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_turkish_case_suffix_keeps_named_target(apostrophe):
    result = normalize_mapping_request(
        "DWH.SRC tablosunu EDW katmanına tip 2 olarak modelle, anahtar ID, "
        f"hedef tablo DWH.DIM_X{apostrophe}e", {})
    assert (result["source_schema"], result["source_table"]) == ("DWH", "SRC")
    assert result["target_layer"] == "EDW"
    assert (result["target_schema"], result["target_table"]) == ("DWH", "DIM_X")
    assert result["load_type"] == "type2"
    assert result["key_columns"] == ["ID"]


@pytest.mark.parametrize(("layer_phrase", "layer", "load_type"), [
    ("ODS'e", "ODS", "insert_update"), ("EDW'ye", "EDW", "insert_update"),
    ("DM'e", "DM", "insert_update"), ("datamart'a", "DM", "insert_update"),
    ("data mart'a", "DM", "insert_update"),
])
def test_turkish_dative_layer_is_a_destination(layer_phrase, layer, load_type):
    result = normalize_mapping_request(
        f"DWH.X tablosunu {layer_phrase} insert update olarak yükle, anahtar ID", {})
    assert result["target_layer"] == layer
    assert result["load_type"] == load_type
    assert result["key_columns"] == ["ID"]


def test_unqualified_identifier_case_suffix_binds_its_marker():
    source = normalize_mapping_request("Model table ORDERS'i into ODS as merge", {})
    target = normalize_mapping_request(
        "Model HR.ORDERS into EDW as type 2, target table DIM_X'e", {})
    assert source["source_table"] == "ORDERS"
    assert target["target_table"] == "DIM_X"


@pytest.mark.parametrize("message", [
    "DWH.F1'brokenlong tablosunu ODS katmanına insert update olarak modelle, anahtar ID",
    "DWH.F1’brokenlong tablosunu ODS katmanına insert update olarak modelle, anahtar ID",
    "Model table BAD;DROP into ODS as merge",
])
def test_invalid_table_marker_does_not_bind_the_next_layer_word(message):
    result = normalize_mapping_request(message, {})
    assert "source_table" not in result
    assert result["target_layer"] == "ODS"
    assert "target_table" not in result


@pytest.mark.parametrize("bad_target", ["DWH.DIM_X'brokenlong", "DWH.DIM_X’brokenlong", "X;DROP"])
def test_invalid_explicit_target_never_falls_back_to_layer_convention(bad_target):
    result = normalize_mapping_request(
        f"DWH.SRC tablosunu EDW katmanına tip 2 olarak modelle, anahtar ID, hedef tablo {bad_target}", {})
    assert result["target_layer"] == "EDW"
    assert result["source_table"] == "SRC"
    assert "target_table" not in result
    assert result["load_type"] == "type2"
    assert result["key_columns"] == ["ID"]


@pytest.mark.parametrize(("tail", "load_type", "keys"), [
    ("yükleme anahtar ID", "insert_update", ["ID"]),
    ("yükleme", "insert_update", None),
    ("yüklemede", "insert_update", None),
    ("yüklemek", "insert_update", None),
    ("istemci anahtar ID", "insert_update", ["ID"]),
    ("key SHOULD_BE", "insert_update", ["SHOULD_BE"]),
    ("key DEGIL_FLAG", "insert_update", ["DEGIL_FLAG"]),
    ("key ISTEM_KODU", "insert_update", ["ISTEM_KODU"]),
    ("silme", "insert_update", None),
    ("ekleme", "insert_update", None),
    ("güncelleme", "insert_update", None),
    ("atama", "insert_update", None),
    ("olma", "insert_update", None),
    ("shoulder", "insert_update", None),
    ("avoidance", "insert_update", None),
])
def test_affirmative_nouns_and_identifier_keys_keep_load_type(tail, load_type, keys):
    result = normalize_mapping_request(
        f"DWH.X tablosunu ODS katmanına insert update olarak modelle, {tail}", {})
    assert result["load_type"] == load_type
    assert result.get("key_columns") == keys


@pytest.mark.parametrize(("message", "expected"), [
    ("DWH.X tablosunu ODS katmanına full reload olarak modelle, yüklemede", "truncate_load"),
    ("DWH.X tablosunu ODS katmanına truncate insert olarak yüklemek", "truncate_load"),
    ("DWH.X tablosunu ODS katmanına ekleme güncelleme olarak modelle, anahtar ID", "insert_update"),
    ("DWH.X tablosunu ODS katmanına ekleme/güncelleme olarak modelle, anahtar ID", "insert_update"),
    ("DWH.X tablosunu ODS katmanına tam yükleme olarak modelle", "truncate_load"),
])
def test_affirmative_load_vocabulary_is_not_a_hedge(message, expected):
    result = normalize_mapping_request(message, {})
    assert result["load_type"] == expected


@pytest.mark.parametrize("negative", [
    "yükleme yapma", "truncate etmeden", "yüklemesin", "silmeyin",
    "kullanmadan", "istemiyorum", "shouldn't", "yapmayacak", "etmemeli",
    "atmayın", "olmadı", "güncellemiyor",
])
def test_precise_negative_forms_still_clear_assigned_load(negative):
    result = normalize_mapping_request(
        f"DWH.X tablosunu ODS katmanına insert update olarak modelle, {negative}", {})
    assert "load_type" not in result


def test_unclosed_quote_leaves_trailing_hedge_visible():
    result = normalize_mapping_request('Model HR.ORDERS into ODS as truncate insert "unbalanced do not', {})
    assert "load_type" not in result


@pytest.mark.parametrize("negative", [
    "yüklenmesin", "silinmesin", "kullanılmasın", "yapılmasın",
    "edilmesin", "yüklenmeden", "silinmeyecek", "kullanılmayacak",
    "yapılmaz", "yaptırmasın", "sildirmeyin", "güncelletmesin",
])
def test_passive_and_causative_negatives_clear_assigned_load(negative):
    result = normalize_mapping_request(
        f"DWH.X tablosunu ODS katmanına truncate insert olarak modelle, {negative}", {})
    assert "load_type" not in result


@pytest.mark.parametrize("affirmative", [
    "yüklensin", "silinsin", "yapılsın", "yüklenecek",
])
def test_affirmative_passives_keep_assigned_load(affirmative):
    result = normalize_mapping_request(
        f"DWH.X tablosunu ODS katmanına insert update olarak modelle, {affirmative}", {})
    assert result["load_type"] == "insert_update"


@pytest.mark.parametrize("hedge", ["gerek yok", "gereksiz", "gerekmez", "gerekmiyor"])
def test_general_refusal_vocabulary_clears_assigned_load(hedge):
    result = normalize_mapping_request(
        f"Model HR.ORDERS into ODS as truncate insert, {hedge}", {})
    assert "load_type" not in result


@pytest.mark.parametrize("hedge", [
    "skip truncate", "skip the truncate", "skipping truncate", "disable truncate",
    "disabled truncate", "omit history", "exclude full reload", "excluding type 2",
    "ignore type 2", "truncate atla", "truncate atlayın", "truncate devre dışı",
    "truncate kapalı", "truncate iptal", "truncate'i atla", "tarihçe devre dışı",
    "truncate devre dışı olsun", "iptal truncate",
])
def test_deleted_refusal_vocabulary_does_not_clear_assigned_load(hedge):
    result = normalize_mapping_request(
        f"Model HR.ORDERS into ODS as truncate insert, {hedge}", {})
    assert result["load_type"] == "truncate_load"


@pytest.mark.parametrize("plain_word", [
    "skip", "skipping", "disable", "disabled", "omit", "exclude", "excluding",
    "ignore", "atla", "atlayın", "devre dışı", "kapalı", "iptal",
])
def test_non_governing_refusal_vocabulary_is_plain_text(plain_word):
    result = normalize_mapping_request(
        f"Model HR.ORDERS into ODS as truncate insert, {plain_word}", {})
    assert result["load_type"] == "truncate_load"


@pytest.mark.parametrize(("message", "load_type", "keys"), [
    ("DWH.X tablosunu ODS katmanına insert update olarak modelle, anahtar ID, "
     "iptal edilen kayıtlar dahil", "insert_update", ["ID"]),
    ("DWH.X tablosunu ODS katmanına insert update olarak modelle, anahtar ID. "
     "Iptal edilen kayıtlar dahil", "insert_update", ["ID"]),
    ("DWH.X tablosunu ODS katmanına insert update olarak modelle, anahtar ID, "
     "kapalı kayıtlar dahil", "insert_update", ["ID"]),
    ("DWH.X tablosunu ODS katmanına insert update olarak modelle, anahtar ID. "
     "Kapalı kayıtlar dahil", "insert_update", ["ID"]),
    ("Model HR.ORDERS into ODS as truncate insert, ignore duplicates", "truncate_load", None),
    ("Model HR.ORDERS into ODS as truncate insert, exclude archived rows", "truncate_load", None),
    ("Model HR.ORDERS into ODS as truncate insert, skip the audit column", "truncate_load", None),
    ("DWH.X tablosunu ODS katmanına insert update olarak modelle, anahtar SKIP",
     "insert_update", ["SKIP"]),
    ("DWH.X tablosunu ODS katmanına insert update olarak modelle, anahtar ID, kolon IGNORE",
     "insert_update", ["ID"]),
])
def test_non_governing_refusal_words_keep_load_and_prior_keys(message, load_type, keys):
    result = normalize_mapping_request(message, {})
    assert result["load_type"] == load_type
    assert result.get("key_columns") == keys


@pytest.mark.parametrize("wording", [
    "key ID, BAD;DROP", "key ID, BAD;DROP OTHER", "key ID, today", "key ORDER ID",
])
def test_invalid_single_key_token_still_drops_keys(wording):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS as merge, {wording}", {})
    assert "key_columns" not in result


@pytest.mark.parametrize(("tail", "load_type"), [
    ("skip the type column", "truncate_load"),
    ("ignore the update flag", "truncate_load"),
    ("exclude the load date", "truncate_load"),
    ("skip the insert date", "truncate_load"),
    ("disable the reload window", "truncate_load"),
    ("ignore the SCD code", "truncate_load"),
    ("atla tip kolonu", "insert_update"),
    ("iptal tip kolonu", "insert_update"),
])
def test_deleted_refusal_words_before_column_descriptions_keep_load(tail, load_type):
    if load_type == "truncate_load":
        message = f"Model HR.ORDERS into ODS as truncate insert, {tail}"
    else:
        message = f"DWH.X tablosunu ODS katmanına insert update olarak modelle, {tail}"
    assert normalize_mapping_request(message, {})["load_type"] == load_type


@pytest.mark.parametrize(("wording", "keys"), [
    ("anahtar ID, STATUS and others", ["ID", "STATUS"]),
    ("key A, B plus C", ["A"]),
    ("anahtar ID, KOD alanı ile", ["ID"]),
    ("keys: A, B and C", ["A", "B", "C"]),
    ("anahtar ID ve KOD", ["ID", "KOD"]),
    ("key id, kod", ["ID", "KOD"]),
    ("key customer_id, kod", ["CUSTOMER_ID"]),
    ("key id2, kod", ["ID2"]),
    ("key ID, Others", ["ID"]),
])
def test_key_list_keeps_first_identifier_typing_style(wording, keys):
    result = normalize_mapping_request(f"Model HR.ORDERS into ODS as merge, {wording}", {})
    assert result["key_columns"] == keys


@pytest.mark.parametrize("affirmative", [
    "yapmak", "etmek", "yapması", "etmesi", "yapmaya", "etmeye",
    "yapmayı", "etmeyi", "yapmada", "etmede", "yapmam gerek",
])
def test_auxiliary_infinitives_and_verbal_nouns_keep_assigned_load(affirmative):
    result = normalize_mapping_request(
        f"DWH.X tablosunu ODS katmanına insert update olarak {affirmative}, anahtar ID", {})
    assert result["load_type"] == "insert_update"
    assert result["key_columns"] == ["ID"]


@pytest.mark.parametrize("possessive", ["customer's", "customer’s", "Customer's", "CUSTOMER'S"])
def test_english_possessive_is_not_a_source_identifier(possessive):
    result = normalize_mapping_request(
        f"Model the {possessive} table into the ODS layer as truncate insert", {})
    assert "source_table" not in result
    assert "target_table" not in result
    assert result["target_layer"] == "ODS"
    assert result["load_type"] == "truncate_load"


@pytest.mark.parametrize("contraction", ["DON'T", "CAN'T", "WON'T", "ISN'T", "DON’T"])
def test_uppercase_english_contractions_remain_hedges(contraction):
    result = normalize_mapping_request(
        f"Model HR.ORDERS into ODS as truncate insert, {contraction}", {})
    assert "load_type" not in result


@pytest.mark.parametrize("source_ref", [
    "F1'i", "F1'ı", "F1'in", "F1'ın", "F1'nin", "F1'deki", "F1'daki",
    "F1'dan", "F1'den", "F1’den",
])
def test_unqualified_case_suffixes_still_bind_table_marker(source_ref):
    result = normalize_mapping_request(f"{source_ref} tablosunu ODS katmanına as merge", {})
    assert result["source_table"] == "F1"
    assert result["target_layer"] == "ODS"
    assert result["load_type"] == "insert_update"


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
