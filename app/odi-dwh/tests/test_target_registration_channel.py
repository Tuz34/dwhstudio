"""Target registration must not depend on a source account's catalog visibility."""

from types import SimpleNamespace

import pandas as pd
import pytest

import mapping_generator
import target_table_service
from mapping_generator import MappingGenerator
from odi_orchestrator import OdiOrchestrator


def _frame(name):
    return pd.DataFrame([{
        "COLUMN_NAME": name, "DATA_TYPE": "NUMBER", "DATA_LENGTH": 22,
        "DATA_PRECISION": 10, "DATA_SCALE": 0, "NULLABLE": "N",
        "FULL_DATA_TYPE": "NUMBER(10)", "COMMENTS": None,
    }])


@pytest.mark.parametrize("target_owner", ["DWH", "DM"])
@pytest.mark.parametrize("internal_source", [False, True])
@pytest.mark.parametrize("source_exists", [False, True])
def test_registration_reads_target_catalog_through_target_service(
    monkeypatch, target_owner, internal_source, source_exists,
):
    source_reads, target_reads, registrations = [], [], []

    def source_schema(table):
        source_reads.append(table)
        if table != "HR.SOURCE_TABLE":
            raise RuntimeError("source account cannot read a target schema")
        return _frame("SOURCE_ID")

    def target_schema(owner, table):
        target_reads.append((owner, table))
        return _frame("TARGET_ID" if table == "TARGET_TABLE" else "SOURCE_ID")

    connector = SimpleNamespace(get_table_schema=source_schema, type_to_oracle=lambda dtype, *args: dtype)
    monkeypatch.setattr(mapping_generator, "get_source_connector", lambda kind: connector)
    monkeypatch.setattr(target_table_service, "read_table_schema", target_schema)
    monkeypatch.setattr(OdiOrchestrator, "register_datastore",
                        lambda self, **kwargs: registrations.append(kwargs) or "SUCCESS")
    generator = MappingGenerator()
    monkeypatch.setattr(generator, "_ensure_odi_repository_bindings", lambda params: None)
    monkeypatch.setattr(generator, "_resolved_odi_source_model_code", lambda params: "SRC_MODEL")
    monkeypatch.setattr(generator, "_resolved_odi_target_model_code", lambda params: "TGT_MODEL")
    params = {
        "src_model": "ORACLE_ODS" if internal_source else "ORACLE_HR",
        "src_owner": "DWH" if internal_source else "HR",
        "src_table": "SOURCE_TABLE" if internal_source else "HR.SOURCE_TABLE",
        "tgt_model": "ORACLE_DWH", "tgt_owner": target_owner, "tgt_table": "TARGET_TABLE",
        "_odi_src_datastore_exists": source_exists, "_odi_tgt_datastore_exists": False,
    }

    result = generator._register_odi_datastores(params)

    assert result["tgt_result"] == "SUCCESS"
    assert source_reads == ([] if internal_source else ["HR.SOURCE_TABLE"])
    assert target_reads == ([('DWH', 'SOURCE_TABLE')] if internal_source else []) + [
        (target_owner, "TARGET_TABLE"),
    ]
    assert len(registrations) == (1 if source_exists else 2)
    target = registrations[-1]
    assert target["model_code"] == "TGT_MODEL" and target["table_name"] == "TARGET_TABLE"
    assert target["columns"] == [{"name": "TARGET_ID", "type": "NUMBER", "length": 10}]


@pytest.mark.parametrize(("column", "expected"), [
    ({"DATA_TYPE": "NUMBER", "MAX_LEN": 22, "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0},
     {"name": "VALUE", "type": "NUMBER", "length": 10}),
    ({"DATA_TYPE": "NUMBER", "MAX_LEN": 22, "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 2},
     {"name": "VALUE", "type": "NUMBER", "length": 10, "scale": 2}),
    ({"DATA_TYPE": "NUMBER", "MAX_LEN": 22, "NUMERIC_PRECISION": None, "NUMERIC_SCALE": None},
     {"name": "VALUE", "type": "NUMBER"}),
    ({"DATA_TYPE": "VARCHAR2", "MAX_LEN": 40},
     {"name": "VALUE", "type": "VARCHAR2", "length": 40}),
])
def test_registration_uses_declared_numeric_precision_not_storage_length(column, expected):
    assert MappingGenerator()._odi_register_column_defs([{"COLUMN_NAME": "VALUE", **column}]) == [expected]
