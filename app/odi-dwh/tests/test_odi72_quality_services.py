from __future__ import annotations

from dataclasses import is_dataclass
from types import SimpleNamespace

import pandas as pd
import pytest

import mapping_detail_presenter
import mapping_run_service
import reconciliation_service
import target_table_service
from exceptions import MappingGenerationError, ReconciliationError


def test_mapping_detail_presenter_has_typed_parse_outcomes():
    assert is_dataclass(mapping_detail_presenter.MappingDetailPresentation)

    invalid = mapping_detail_presenter.present_mapping_detail("MAP_X", "not-json", lambda _name: None, lambda _name: {})
    raw = mapping_detail_presenter.present_mapping_detail("MAP_X", '{"status":"ok"}', lambda _name: None, lambda _name: {})

    assert invalid.kind == "parse_error"
    assert invalid.raw_json == "not-json"
    assert raw.kind == "raw"


def test_mapping_detail_schema_enrichment_is_best_effort_without_leaking_exception():
    rows = [{
        "COMP_NAME": "ODS_X",
        "COMP_TYPE": "DATASTORE",
        "TABLE_QUALIFIED_NAME": "DWH.ODS_X",
        "ATTR_NAME": "ID",
        "EXPRESSION": "SRC.ID",
        "DATA_TYPE": "NUMBER",
    }]

    def unavailable_schema(_name):
        raise RuntimeError("credential detail must not escape")

    result = mapping_detail_presenter.present_mapping_detail(
        "MAP_X",
        __import__("json").dumps(rows),
        unavailable_schema,
        lambda _name: {"read_only": True},
    )

    assert result.kind == "payload"
    assert "credential detail" not in str(result.payload)


def test_mapping_run_validation_requires_literal_boolean_and_typed_outcome():
    assert is_dataclass(mapping_run_service.MappingRunOutcome)
    invalid, error = mapping_run_service.validate_mapping_run_options(
        {"truncate_target_table": 1}, "MAP_X", lambda _value: True, lambda _value: True
    )
    valid, valid_error = mapping_run_service.validate_mapping_run_options(
        {"truncate_target_table": False}, "MAP_X", lambda _value: True, lambda _value: True
    )

    assert invalid is None
    assert "truncate_target_table" in error
    assert valid_error is None
    assert valid is not None and valid.truncate_target_table is False


def _reconciliation_deps(connection_factory):
    return reconciliation_service.ReconciliationDependencies(
        get_connection=connection_factory,
        close_resource=lambda resource, _label: resource.close() if resource else None,
        parse_source_table=lambda _table: ("HR", "EMPLOYEES"),
        source_schema_columns=lambda _schema, _table: {},
        source_count=lambda _schema, _table: 0,
        source_sample_rows=lambda *_args: pd.DataFrame(),
        infer_pk_columns=lambda *_args: [],
        q_oracle=lambda name: f'"{name}"',
        pk_key=lambda _row, _columns: "",
        is_lob_type=lambda _props: False,
        norm_value=lambda value: value,
        values_equal=lambda *_args: True,
        map_sql_to_oracle_type=lambda value: value,
        compare_data=lambda *_args: reconciliation_service._empty_compare(),
        tech_columns=set(),
        compare_limit=10,
    )


def test_reconciliation_domain_error_preserves_original_cause():
    original = RuntimeError("internal connection detail")

    def broken_connection():
        raise original

    with pytest.raises(ReconciliationError) as captured:
        reconciliation_service._read_target_metadata("DWH", "ODS_X", _reconciliation_deps(broken_connection))

    assert captured.value.__cause__ is original
    assert "internal connection detail" not in str(captured.value)


def test_target_table_domain_error_preserves_original_cause(monkeypatch):
    original = RuntimeError("internal DDL detail")
    monkeypatch.setattr(target_table_service, "_connection", lambda: (_ for _ in ()).throw(original))
    context = target_table_service.TargetTableContext("DWH", "ODS_X", False, "APP_USER")

    with pytest.raises(MappingGenerationError) as captured:
        target_table_service._target_exists(context)

    assert captured.value.__cause__ is original
    assert "internal DDL detail" not in str(captured.value)


def test_target_table_existing_plan_returns_metadata_only(monkeypatch):
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _context: True)
    monkeypatch.setattr(target_table_service, "_target_columns", lambda _context: {"ID"})
    logs = []
    generator = SimpleNamespace(_add_log=lambda *args: logs.append(args))

    result = target_table_service.execute_target_table_plan(
        generator,
        {"tgt_owner": "DWH", "tgt_table": "ODS_X", "drop_target_if_exists": False},
        {"src_columns": [{"COLUMN_NAME": "ID"}], "desired_col_defs": [("ID", "NUMBER")], "defaults_added": [], "col_defs": []},
        "APP_USER",
    )

    assert result["status"] == "ALREADY_IN_SYNC"
    assert result["added_columns"] == []
    assert "raw_rows" not in result
    assert logs[-1][1] == "SUCCESS"
