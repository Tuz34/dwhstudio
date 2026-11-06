"""The real fresh-target service result must satisfy the application driver."""

import pytest

import odi81_app_acceptance as driver
import target_table_service
from mapping_generator import MappingGenerator


SOURCE_COLUMNS = [
    {"COLUMN_NAME": "ENTITY_ID", "ORACLE_TYPE": "NUMBER(10)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "ATTR_TEXT", "ORACLE_TYPE": "VARCHAR2(40 BYTE)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "VALUE_AMT", "ORACLE_TYPE": "NUMBER(10,2)", "IS_NULLABLE": "N"},
]


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_real_created_target_result_is_accepted_without_fabricated_key_kind(monkeypatch, flow):
    generator = MappingGenerator()
    monkeypatch.setattr(generator, "_source_columns_for", lambda params: SOURCE_COLUMNS)
    params = generator.validate_params(driver.classic_params(flow))
    statements = []
    monkeypatch.setattr(target_table_service, "_target_exists", lambda context: False)
    monkeypatch.setattr(target_table_service, "_create_target",
                        lambda context, definitions: statements.append(definitions) or "synthetic DDL")
    monkeypatch.setattr(target_table_service, "_apply_grants", lambda context: None)
    monkeypatch.setattr(target_table_service, "_write_creation_audit", lambda *args: None)

    outcome = generator._create_target_table(params)
    payload = {"overall_status": "SUCCESS", "params": params,
               "steps": {"create_target_table": outcome}}

    driver._accept_classic_generate(flow, 200, payload)
    assert outcome["status"] == "CREATED" and len(statements) == 1
    if flow == "F2":
        assert outcome["key"] == {"status": "NONE", "columns": []}
        assert not any("PRIMARY KEY" in definition for definition in statements[0])
    else:
        assert outcome["key"]["kind"] == "primary"
        assert outcome["key"]["columns"] == ["ENTITY_ID"]
        assert sum('PRIMARY KEY ("ENTITY_ID")' in definition for definition in statements[0]) == 1


@pytest.mark.parametrize("kind", ["primary", "unique"])
def test_created_key_reports_the_kind_of_the_actual_ddl(monkeypatch, kind):
    generator = MappingGenerator()
    params = {"tgt_owner": "DWH", "tgt_table": "FIXED_TEST_TARGET"}
    plan = {"src_columns": [{"COLUMN_NAME": "ID"}], "col_defs": ['    "ID" NUMBER'],
            "desired_col_defs": [("ID", "NUMBER")], "defaults_added": [],
            f"{kind}_key": ["ID"]}
    statements = []
    monkeypatch.setattr(target_table_service, "_target_exists", lambda context: False)
    monkeypatch.setattr(target_table_service, "_create_target",
                        lambda context, definitions: statements.append(definitions) or "synthetic DDL")
    monkeypatch.setattr(target_table_service, "_apply_grants", lambda context: None)
    monkeypatch.setattr(target_table_service, "_write_creation_audit", lambda *args: None)

    outcome = target_table_service.execute_target_table_plan(generator, params, plan, "DWH")

    assert outcome["key"]["kind"] == kind
    keyword = "PRIMARY KEY" if kind == "primary" else "UNIQUE"
    assert sum(f'{keyword} ("ID")' in definition for definition in statements[0]) == 1
