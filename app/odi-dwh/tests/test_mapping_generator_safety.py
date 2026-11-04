from __future__ import annotations

import pytest


BASE_PARAMS = {
    "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
    "src_model": "ORACLE_HR",
    "src_table": "HR.EMPLOYEES",
    "tgt_model": "ORACLE_ODS",
    "tgt_owner": "DWH",
    "tgt_table": "ODS_HR_EMPLOYEES",
    "update_key_columns": ["EMPLOYEE_ID"],
}


@pytest.fixture(autouse=True)
def configured_schemas(monkeypatch):
    monkeypatch.setattr("mapping_generator.SOURCE_ORACLE_SCHEMAS", ["HR", "SH"])


@pytest.mark.parametrize(
    "field",
    ["drop_target_if_exists", "replace_existing_mapping", "needs_skey"],
)
def test_destructive_mapping_flags_accept_only_literal_true(field):
    from mapping_generator import MappingGenerator

    params = {**BASE_PARAMS, field: "false"}
    validated = MappingGenerator().validate_params(params)

    assert validated[field] is False


@pytest.mark.parametrize(
    "field",
    ["drop_target_if_exists", "replace_existing_mapping", "needs_skey"],
)
def test_destructive_mapping_flags_preserve_literal_true(field):
    from mapping_generator import MappingGenerator

    params = {**BASE_PARAMS, field: True}
    if field == "needs_skey":
        params["skey_natural_key"] = "EMPLOYEE_ID"

    validated = MappingGenerator().validate_params(params)

    assert validated[field] is True
