"""Execute the derived classic audit CASE expressions. Not a native ODI run."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mapping_generator
import odi81_app_acceptance as driver
from mapping_generator import (
    AuditPreservationRejected,
    MappingGenerator,
    _SESSION_BATCH_SQL,
    build_same_schema_audit_expressions,
)
from odi_orchestrator import OdiOrchestrator, _EXPR_VAL_RE

_SOURCE_COLUMNS = (
    {"COLUMN_NAME": "ENTITY_ID", "ORACLE_TYPE": "NUMBER(10)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "ATTR_TEXT", "ORACLE_TYPE": "VARCHAR2(40 BYTE)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "VALUE_AMT", "ORACLE_TYPE": "NUMBER(10,2)", "IS_NULLABLE": "N"},
)
_BATCH_VALUE = '<%=odiRef.getSession("SESS_NO")%>'


def _generator(flow: str) -> tuple[MappingGenerator, dict, dict]:
    generator = MappingGenerator()
    generator._source_columns_for = lambda _params: [dict(column) for column in _SOURCE_COLUMNS]
    validated = generator.validate_params(driver.classic_params(flow))
    plan = generator._build_target_column_plan(validated)
    return generator, validated, plan


def _profile(flow: str) -> dict:
    generator, validated, plan = _generator(flow)
    return generator._prepare_audit_preservation(validated, plan)


def _to_number(value: object) -> None:
    raise sqlite3.DataError("ambiguous key")


def _register(connection: sqlite3.Connection) -> None:
    connection.create_function("TO_DATE", 2, lambda value, _fmt: value)
    connection.create_function(
        "TRUNC", 1, lambda value: None if value is None else str(value)[:10],
    )
    connection.create_function("TO_NUMBER", 1, _to_number)


def evaluate_audit_rows(main: sqlite3.Connection, owner: str, source: str, target: str,
                        expressions: dict[str, str], source_columns: tuple[str, ...]) -> list[tuple]:
    """Run the builder's SQL text against a copy of the tables."""
    side = sqlite3.connect(":memory:")
    try:
        _register(side)
        side.execute(f"ATTACH DATABASE ':memory:' AS {owner}")
        for name in (source, target):
            info = main.execute(f'PRAGMA table_info("{name}")').fetchall()
            rows = main.execute(f'SELECT * FROM "{name}"').fetchall()
            definitions = ",".join(f'"{column[1]}" {column[2] or "TEXT"}' for column in info)
            side.execute(f'CREATE TABLE "{owner}"."{name}" ({definitions})')
            if rows:
                placeholders = ",".join("?" for _ in info)
                side.executemany(
                    f'INSERT INTO "{owner}"."{name}" VALUES ({placeholders})', rows,
                )
        selected = ", ".join(f'({expressions[name]})' for name in expressions)
        business = ", ".join(f'SRC."{name}"' for name in source_columns)
        return side.execute(
            f'SELECT {business}, {selected} FROM "{owner}"."{source}" SRC ORDER BY SRC."{source_columns[0]}"'
        ).fetchall()
    finally:
        side.close()


def _prepare_pair(connection: sqlite3.Connection, source: str, target: str,
                  source_columns: tuple[str, ...], audit_columns: tuple[str, ...],
                  source_rows: list[tuple], target_rows: list[tuple]) -> None:
    columns = source_columns + audit_columns
    width = len(columns)
    for name, rows in ((source, source_rows), (target, target_rows)):
        quoted = ",".join(f'"{column}" TEXT' for column in columns)
        connection.execute(f'CREATE TABLE "{name}" ({quoted})')
        padded = [tuple(row) + (None,) * (width - len(row)) for row in rows]
        if padded:
            placeholders = ",".join("?" for _ in columns)
            connection.executemany(
                f'INSERT INTO "{name}" VALUES ({placeholders})', padded,
            )


def test_same_schema_iu_builds_executable_null_safe_cases():
    profile = _profile("F1")
    assert profile["qualified"] is True
    assert profile["expression_count"] == 4
    assert profile["business_column_count"] == 2
    assert profile["key_count"] == 1
    expressions = profile["expressions"]
    assert set(expressions) == {"INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"}
    for expression in expressions.values():
        assert "ROWNUM" not in expression and "TARGET." not in expression and "MAX(" not in expression
        assert 'SRC."ENTITY_ID"' in expression and '"DWH"."ODS_ODI81_F1"' in expression
    assert "TO_NUMBER('AMBIGUOUS')" in expressions["INSERT_DATE"]
    assert "CURRENT_TIMESTAMP" in expressions["UPDATE_DATE"]
    assert _SESSION_BATCH_SQL in expressions["ETL_BATCH_ID"]
    blob = json.dumps({key: profile[key] for key in profile if key != "expressions"})
    assert "CASE" not in blob and "SELECT" not in blob


def test_f2_cross_schema_and_join_keep_the_existing_mode():
    f2 = _profile("F2")
    assert f2["qualified"] is False and f2["expressions"] == {}
    denied = build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="ODS", src_model="ORACLE_DWH",
        tgt_model="ORACLE_ODS", tgt_table="ODS_ODI81_F1", update_key_columns=["ENTITY_ID"],
        source_columns=["ENTITY_ID", "ATTR_TEXT"], target_columns=["ENTITY_ID", "ATTR_TEXT", "INSERT_DATE"],
        source_system_literal="ORACLE_SAMPLE_SCHEMAS",
    )
    assert denied["qualified"] is False and denied["expressions"] == {}
    joined = build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="ORACLE_DWH",
        tgt_model="ORACLE_ODS", tgt_table="ODS_ODI81_F1", update_key_columns=["ENTITY_ID"],
        source_columns=["ENTITY_ID", "ATTR_TEXT"], target_columns=["ENTITY_ID", "ATTR_TEXT", "UPDATE_DATE"],
        source_system_literal="ORACLE_SAMPLE_SCHEMAS", join_table="OTHER",
    )
    assert joined["qualified"] is False
    remote = build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="SQL_SERVER",
        tgt_model="ORACLE_ODS", tgt_table="ODS_REMOTE", update_key_columns=["ENTITY_ID"],
        source_columns=["ENTITY_ID"], target_columns=["ENTITY_ID", "UPDATE_DATE"],
        source_system_literal="ORACLE_SAMPLE_SCHEMAS",
    )
    assert remote["qualified"] is False


def test_executed_sql_preserves_nulls_and_fails_closed_on_ambiguity():
    profile = _profile("F1")
    expressions = profile["expressions"]
    main = sqlite3.connect(":memory:")
    source_columns = ("ENTITY_ID", "ATTR_TEXT", "VALUE_AMT")
    audit_columns = tuple(expressions)
    _prepare_pair(
        main, "SRC_F1", "TGT_F1", source_columns, audit_columns,
        [("101", "SAME", "10", None, None, None, None),
         ("102", "EDIT", "20", None, None, None, None),
         ("103", "NEW", "30", None, None, None, None)],
        [("101", "SAME", "10", None, None, "OLD_SRC", None),
         ("102", "OLD", None, "OLD_INS", None, None, "OLD_BATCH")],
    )
    # The qualified SQL names the real F1 table. Point this execution at that name.
    main.execute('ALTER TABLE "TGT_F1" RENAME TO "ODS_ODI81_F1"')
    main.execute('ALTER TABLE "SRC_F1" RENAME TO "ODI81_SRC_F1"')
    rows = evaluate_audit_rows(
        main, "DWH", "ODI81_SRC_F1", "ODS_ODI81_F1", expressions, source_columns,
    )
    by_id = {row[0]: row for row in rows}
    unchanged = by_id["101"]
    changed = by_id["102"]
    created = by_id["103"]
    assert unchanged[3:] == (None, None, "OLD_SRC", None)
    assert changed[3] == "OLD_INS"
    assert changed[5] is None
    assert changed[4] != "OLD_INS" and changed[4] is not None
    assert changed[6] == _BATCH_VALUE
    assert created[3] is not None and created[4] == created[3]
    assert created[6] == _BATCH_VALUE
    main.execute(
        'INSERT INTO "ODS_ODI81_F1" VALUES (?,?,?,?,?,?,?)',
        ("101", "SAME", "10", "SECOND", "SECOND", "SECOND", "SECOND"),
    )
    with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
        evaluate_audit_rows(main, "DWH", "ODI81_SRC_F1", "ODS_ODI81_F1", expressions, source_columns)


def test_composite_null_key_matches_only_the_same_nulls():
    expressions = build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="ORACLE_DWH",
        tgt_model="ORACLE_DWH", tgt_table="DWH_ODI81_F3", update_key_columns=["ENTITY_ID", "ATTR_TEXT"],
        source_columns=["ENTITY_ID", "ATTR_TEXT", "VALUE_AMT"],
        target_columns=["ENTITY_ID", "ATTR_TEXT", "VALUE_AMT", "INSERT_DATE", "UPDATE_DATE",
                        "CURRENT_FLAG", "SOURCE_SYSTEM", "ETL_BATCH_ID"],
        source_system_literal="ORACLE_SAMPLE_SCHEMAS",
    )
    assert expressions["qualified"] is True
    assert expressions["key_count"] == 2
    assert "CURRENT_FLAG" in expressions["expressions"]
    body = expressions["expressions"]["UPDATE_DATE"]
    assert 'SRC."ATTR_TEXT"' in body and "ROWNUM" not in body
    main = sqlite3.connect(":memory:")
    columns = ("ENTITY_ID", "ATTR_TEXT", "VALUE_AMT")
    audit = ("INSERT_DATE", "UPDATE_DATE", "CURRENT_FLAG", "SOURCE_SYSTEM", "ETL_BATCH_ID")
    _prepare_pair(
        main, "SRC3", "TGT3", columns, audit,
        [("7", None, "1"), ("7", "B", "2"), ("8", None, "9")],
        [("7", None, "1", "KEEP_INS", None, "Y", "KEEP_SRC", None),
         ("7", "B", "1", "KEEP_B", "OLD_UPD", "Y", "KEEP_SRC", "OLD_BATCH")],
    )
    main.execute('ALTER TABLE "TGT3" RENAME TO "DWH_ODI81_F3"')
    main.execute('ALTER TABLE "SRC3" RENAME TO "ODI81_SRC_F3"')
    rows = evaluate_audit_rows(
        main, "DWH", "ODI81_SRC_F3", "DWH_ODI81_F3", expressions["expressions"], columns,
    )
    null_key = next(row for row in rows if row[0] == "7" and row[1] is None)
    changed = next(row for row in rows if row[0] == "7" and row[1] == "B")
    created = next(row for row in rows if row[0] == "8")
    assert null_key[3:] == ("KEEP_INS", None, "Y", "KEEP_SRC", None)
    assert changed[3] == "KEEP_B" and changed[4] != "OLD_UPD" and changed[6] == "KEEP_SRC"
    assert changed[7] == _BATCH_VALUE
    assert created[5] == "Y" and created[6] == "ORACLE_SAMPLE_SCHEMAS"


def test_mapping_builder_transports_counts_without_the_expression_body(monkeypatch):
    generator, validated, plan = _generator("F3")
    profile = generator._prepare_audit_preservation(validated, plan)
    assert profile["expression_count"] == 7
    assert all("CASE" not in entry["detail"] and "SELECT" not in entry["detail"]
               for entry in generator._log_entries)
    captured = {}
    original_generate = OdiOrchestrator.generate_mapping

    def fake(self, **kwargs):
        captured.update(kwargs)
        return (
            "SUCCESS: Mapping created\n"
            '{"status":"passed","action":"create_mapping","replaced":false,'
            '"kept_existing_on_failure":false}'
        )

    monkeypatch.setattr(OdiOrchestrator, "generate_mapping", fake)
    validated.update({
        "_odi_repository": {"mapping_exists": False},
        "_odi_src_model_code": "ORACLE_DWH",
        "_odi_tgt_model_code": "ORACLE_DWH",
        "_odi_src_datastore_name": "ODI81_SRC_F3",
        "_odi_tgt_datastore_name": "DWH_ODI81_F3",
    })
    monkeypatch.setattr(generator, "_ensure_odi_repository_bindings", lambda _params: validated["_odi_repository"])
    created = generator._create_odi_mapping(validated)
    assert created["audit_preservation"]["expression_count"] == 7
    assert "CASE" not in json.dumps(created["audit_preservation"])
    assert captured["audit_preservation_columns"] == list(profile["expressions"])
    assert "SKEY" not in captured["expressions"]
    calls = []
    monkeypatch.setattr(OdiOrchestrator, "generate_mapping", original_generate)
    monkeypatch.setattr(
        OdiOrchestrator, "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )
    OdiOrchestrator().generate_mapping(
        validated["mapping_name"], validated["src_model"], validated["src_table"],
        validated["tgt_model"], validated["tgt_table"], expressions=captured["expressions"],
        integration_type="IU", audit_preservation_columns=captured["audit_preservation_columns"],
        required_update_key_columns=["ENTITY_ID"], required_update_key_type="PRIMARY_KEY",
    )
    params = calls[0][1]
    assert calls[0][0] == "create_mapping.groovy"
    assert _EXPR_VAL_RE.fullmatch(params["odi.expressions"])
    assert params["odi.audit_preservation_columns"].split(",") == captured["audit_preservation_columns"]
    assert "ROWNUM" not in params["odi.expressions"]
    script = Path(mapping_generator.__file__).resolve().parent / "odi_automation" / "create_mapping.groovy"
    text = script.read_text(encoding="utf-8")
    assert "AUDIT PRESERVATION: applied count=" in text
    assert "AUDIT PRESERVATION: skipped count=" in text
    assert "CUSTOM EXPRESSIONS: ${customExpressions}" not in text


def test_unsafe_expression_is_rejected_before_a_profile_is_returned(monkeypatch):
    generator, validated, plan = _generator("F1")
    import odi_orchestrator
    monkeypatch.setattr(odi_orchestrator, "_EXPR_VAL_RE", __import__("re").compile(r"^$"))
    with pytest.raises(AuditPreservationRejected):
        generator._prepare_audit_preservation(validated, plan)


def test_actual_run_service_success_still_satisfies_the_classic_driver(monkeypatch):
    from types import SimpleNamespace
    import mapping_run_service as service
    import odi_orchestrator
    from tests.test_api_endpoints import _RemoteRunDouble

    class Orchestrator(_RemoteRunDouble):
        def update_mapping(self, **_kwargs):
            return "SUCCESS: options updated"

    monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", Orchestrator)
    monkeypatch.setattr(service, "_persist_run_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_create_failure_alert", lambda *_args, **_kwargs: None)
    options = service.MappingRunOptions("MAP_ODI81_F1", "001", "GLOBAL", False)
    with odi_orchestrator.groovy_approval_context(True):
        outcome = service.execute_mapping_run(SimpleNamespace(), options)
    assert outcome.status_code == 200 and outcome.payload["context"] == "GLOBAL"
    driver._accept_classic_run("F1", "GLOBAL", outcome.status_code, outcome.payload)
