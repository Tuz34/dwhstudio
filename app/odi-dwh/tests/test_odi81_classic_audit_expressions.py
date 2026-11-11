"""Execute the derived classic audit CASE expressions. Not a native ODI run."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

import mapping_generator
import odi81_app_acceptance as driver
from mapping_generator import (
    AuditPreservationRejected,
    MappingGenerator,
    SourceTableNotFound,
    _SESSION_BATCH_SQL,
    _canonical_audit_type,
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


def _to_number(value: object) -> str:
    text = "" if value is None else str(value)
    if text.upper() == "AMBIGUOUS":
        raise sqlite3.DataError("ambiguous key")
    return text


def _register(connection: sqlite3.Connection) -> None:
    connection.create_function("TO_DATE", 2, lambda value, _fmt: value)
    connection.create_function("TO_CHAR", 2, lambda value, _fmt: None if value is None else str(value))
    connection.create_function("TO_TIMESTAMP", 2, lambda value, _fmt: value)
    connection.create_function(
        "TRUNC", 1, lambda value: None if value is None else str(value)[:10],
    )
    connection.create_function("TO_NUMBER", 1, _to_number)
    # Production emits CAST(... AS TIMESTAMP[(n)] [WITH ...] ZONE). SQLite gives
    # that CAST numeric affinity and turns the timestamp into a year. The UDF
    # keeps the value. It is not part of the Oracle text the assertions check.
    connection.create_function("ODI_AUDIT_DATETIME", 1, lambda value: value)


_DATETIME_CAST_TYPE_RE = re.compile(
    r"DATE|TIMESTAMP(?:\(\d+\))?(?: WITH LOCAL TIME ZONE| WITH TIME ZONE)?"
)


def _sqlite_sql(expression: str) -> str:
    """Rewrite Oracle datetime CASTs so the SQLite double can execute them."""
    out: list[str] = []
    index = 0
    upper = expression.upper()
    while True:
        at = upper.find("CAST(", index)
        if at < 0:
            out.append(expression[index:])
            break
        depth = 1
        cursor = at + 5
        while cursor < len(expression) and depth:
            char = expression[cursor]
            if char == "'":
                cursor += 1
                while cursor < len(expression):
                    if expression[cursor] == "'" and cursor + 1 < len(expression) and expression[cursor + 1] == "'":
                        cursor += 2
                        continue
                    if expression[cursor] == "'":
                        cursor += 1
                        break
                    cursor += 1
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        if depth:
            out.append(expression[index:])
            break
        interior = expression[at + 5:cursor - 1]
        marker = interior.upper().rfind(" AS ")
        if marker < 0:
            out.append(expression[index:cursor])
            index = cursor
            continue
        inner = _sqlite_sql(interior[:marker])
        declared = interior[marker + 4:].strip()
        if _DATETIME_CAST_TYPE_RE.fullmatch(declared.upper()):
            out.append(expression[index:at] + f"ODI_AUDIT_DATETIME({inner})")
        else:
            out.append(expression[index:at] + f"CAST({inner} AS {declared})")
        index = cursor
    return "".join(out)


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
        selected = ", ".join(f'({_sqlite_sql(expressions[name])})' for name in expressions)
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
        assert 'SRC."ENTITY_ID"' in expression and '"DWH"."ODS_ODI81N_F1"' in expression
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
        tgt_model="ORACLE_ODS", tgt_table="ODS_ODI81N_F1", update_key_columns=["ENTITY_ID"],
        source_columns=["ENTITY_ID", "ATTR_TEXT"], target_columns=["ENTITY_ID", "ATTR_TEXT", "INSERT_DATE"],
        source_system_literal="ORACLE_SAMPLE_SCHEMAS",
    )
    assert denied["qualified"] is False and denied["expressions"] == {}
    joined = build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="ORACLE_DWH",
        tgt_model="ORACLE_ODS", tgt_table="ODS_ODI81N_F1", update_key_columns=["ENTITY_ID"],
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
    main.execute('ALTER TABLE "TGT_F1" RENAME TO "ODS_ODI81N_F1"')
    main.execute('ALTER TABLE "SRC_F1" RENAME TO "ODI81N_SRC_F1"')
    rows = evaluate_audit_rows(
        main, "DWH", "ODI81N_SRC_F1", "ODS_ODI81N_F1", expressions, source_columns,
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
    assert isinstance(created[3], str) and "-" in created[3] and ":" in created[3]
    assert created[4] == created[3]
    assert created[6] == _BATCH_VALUE
    main.execute(
        'INSERT INTO "ODS_ODI81N_F1" VALUES (?,?,?,?,?,?,?)',
        ("101", "SAME", "10", "SECOND", "SECOND", "SECOND", "SECOND"),
    )
    with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
        evaluate_audit_rows(main, "DWH", "ODI81N_SRC_F1", "ODS_ODI81N_F1", expressions, source_columns)


def test_composite_null_key_matches_only_the_same_nulls():
    expressions = build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="ORACLE_DWH",
        tgt_model="ORACLE_DWH", tgt_table="DWH_ODI81N_F3", update_key_columns=["ENTITY_ID", "ATTR_TEXT"],
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
    main.execute('ALTER TABLE "TGT3" RENAME TO "DWH_ODI81N_F3"')
    main.execute('ALTER TABLE "SRC3" RENAME TO "ODI81N_SRC_F3"')
    rows = evaluate_audit_rows(
        main, "DWH", "ODI81N_SRC_F3", "DWH_ODI81N_F3", expressions["expressions"], columns,
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
        "_odi_src_datastore_name": "ODI81N_SRC_F3",
        "_odi_tgt_datastore_name": "DWH_ODI81N_F3",
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
        audit_lookup_alias=profile["lookup_alias"],
        required_update_key_columns=["ENTITY_ID"], required_update_key_type="PRIMARY_KEY",
    )
    params = calls[0][1]
    assert calls[0][0] == "create_mapping.groovy"
    assert _EXPR_VAL_RE.fullmatch(params["odi.expressions"])
    assert params["odi.audit_preservation_columns"].split(",") == captured["audit_preservation_columns"]
    assert params["odi.audit_lookup_alias"] == profile["lookup_alias"]
    assert "ROWNUM" not in params["odi.expressions"]
    script = Path(mapping_generator.__file__).resolve().parent / "odi_automation" / "create_mapping.groovy"
    text = script.read_text(encoding="utf-8")
    assert "AUDIT PRESERVATION: applied count=" in text
    assert "Audit preservation logical schema does not match the selected profile" in text
    assert "Audit preservation source alias collides with the lookup alias" in text
    assert "customExpressions.remove(auditName)" not in text
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
    options = service.MappingRunOptions("MAP_ODI81N_F1", "001", "GLOBAL", False)
    with odi_orchestrator.groovy_approval_context(True):
        outcome = service.execute_mapping_run(SimpleNamespace(), options)
    assert outcome.status_code == 200 and outcome.payload["context"] == "GLOBAL"
    driver._accept_classic_run("F1", "GLOBAL", outcome.status_code, outcome.payload)


def _mapping_script() -> str:
    return (
        Path(mapping_generator.__file__).resolve().parent / "odi_automation" / "create_mapping.groovy"
    ).read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    at = text.index(start)
    return text[at:text.index(end, at)]


def _groovy_literal(value: str) -> str:
    if "'''" in value:
        raise AssertionError("expression fixture is not embeddable")
    return "'''" + value + "'''"


def _attr_call(item: object) -> str:
    """Groovy Attr literal. A string is a name. A dict may carry an ODI datatype."""
    if isinstance(item, str):
        return f'new Attr(name: "{item}")'
    name = str(item["name"])
    fields = [f'name: "{name}"']
    code = item.get("code")
    if code:
        fields.append(f'dataType: new TypeCode(code: "{code}")')
    if "length" in item:
        fields.append(f"length: {int(item['length'])}")
    if "scale" in item:
        fields.append(f"scale: {int(item['scale'])}")
    return "new Attr(" + ", ".join(fields) + ")"


def _run_mapping_assignment(tmp_path: Path, spec: dict) -> dict:
    """Execute the real rewrite, qualification, and setExpressionText block."""
    from tests import dim_product_mapping_fake_sdk as sdk

    java = sdk.java_binary()
    if java is None or not sdk.GROOVY_JAR.is_file():
        pytest.skip("Local Java/Groovy unavailable; classic audit SDK block is not live-proven")
    source = _mapping_script()
    schema = _between(source, "def sameLogicalSchema = { a, b ->", "// Bounded, redacted projection")
    block = _between(
        source,
        '    String expressionsStr = System.getProperty("odi.expressions")',
        "    // Physical Design & KM Setting",
    )
    properties = []
    for key in ("odi.expressions", "odi.audit_preservation_columns", "odi.audit_lookup_alias"):
        value = spec.get(key)
        if value is None:
            properties.append(f'System.clearProperty("{key}")')
        else:
            properties.append(f'System.setProperty("{key}", {_groovy_literal(value)})')
    source_attributes = ", ".join(
        _attr_call(name) for name in spec.get("source_attributes", ("ENTITY_ID", "ATTR_TEXT"))
    )
    target_attributes = ", ".join(
        _attr_call(name) for name in spec.get("target_attributes", ("ENTITY_ID", "INSERT_DATE"))
    )
    keys = ", ".join(f'"{key}"' for key in spec.get("keys", ("ENTITY_ID",)))
    harness = f'''
import groovy.json.JsonOutput
class LogicalSchema {{
    Object internalId
    String schemaName
    Object getInternalId() {{ internalId }}
    String getName() {{ schemaName }}
}}
class ModelHolder {{
    LogicalSchema logical
    LogicalSchema getLogicalSchema() {{ logical }}
}}
class StoreHolder {{
    ModelHolder model
    ModelHolder getModel() {{ model }}
}}
class TypeCode {{
    String code
    String getCode() {{ code }}
}}
class Attr {{
    String name
    String expression
    Object dataType
    Object length
    Object scale
    String getName() {{ name }}
    Object getDataType() {{ dataType }}
    Object getLength() {{ length }}
    Object getScale() {{ scale }}
    void setExpressionText(String text) {{ expression = text }}
    void setUpdateIndicator(boolean value) {{}}
    void setIsUpdate(boolean value) {{}}
    void setAllowsUpdate(boolean value) {{}}
}}
class Comp {{
    String name
    List attributes = []
    String getName() {{ name }}
    List getAttributes() {{ attributes }}
}}
{schema}
def makeStore = {{ id, name ->
    new StoreHolder(model: new ModelHolder(logical: new LogicalSchema(internalId: id, schemaName: name)))
}}
def srcDS = makeStore({spec["src_schema"][0]}, "{spec["src_schema"][1]}")
def tgtDS = makeStore({spec["tgt_schema"][0]}, "{spec["tgt_schema"][1]}")
def srcComp = new Comp(name: "{spec["source_name"]}", attributes: [{source_attributes}])
def tgtComp = new Comp(name: "TARGET_COMP", attributes: [{target_attributes}])
def joinSrcComp = null
def integrationTypeParam = "{spec["integration"]}"
def ukColNames = [{keys}] as Set
def sourceSystemName = "ORACLE_SAMPLE_SCHEMAS"
{chr(10).join(properties)}
boolean rejected = false
String errorClass = ""
String errorText = ""
try {{
{block}
}} catch (Throwable raised) {{
    rejected = raised instanceof IllegalStateException
    errorClass = raised.getClass().getSimpleName()
    errorText = String.valueOf(raised.getMessage())
}}
def assigned = [:]
tgtComp.attributes.each {{ assigned[it.name] = it.expression }}
println JsonOutput.toJson([rejected: rejected, error_class: errorClass, error_text: errorText, assigned: assigned])
'''
    path = tmp_path / "audit_assignment.groovy"
    path.write_text(harness, encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP", "PATH") if key in os.environ}
    done = subprocess.run(
        [java, "-cp", str(sdk.GROOVY_JAR), "groovy.ui.GroovyMain", str(path)],
        capture_output=True, text=True, timeout=60, check=False, env=environment,
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr[-2000:] or done.stdout[-2000:])
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    report = json.loads(lines[-1])
    report["operator_output"] = "\n".join(lines[:-1])
    return report


def _new_key_value(expression: str, source_name: str) -> object:
    connection = sqlite3.connect(":memory:")
    try:
        _register(connection)
        connection.execute("ATTACH DATABASE ':memory:' AS DWH")
        connection.execute('CREATE TABLE DWH.TGT_SYNTHETIC (ENTITY_ID, ATTR_TEXT, INSERT_DATE)')
        connection.execute("INSERT INTO DWH.TGT_SYNTHETIC VALUES (1,'OLD','OLD_AUDIT')")
        connection.execute(f'CREATE TABLE DWH."{source_name}" (ENTITY_ID, ATTR_TEXT)')
        connection.execute(f'INSERT INTO DWH."{source_name}" VALUES (2,\'NEW\')')
        return connection.execute(
            f'SELECT {_sqlite_sql(expression)} FROM DWH."{source_name}" {source_name}'
        ).fetchone()[0]
    finally:
        connection.close()


def _profile_for(source_table: str | None) -> dict:
    return build_same_schema_audit_expressions(
        integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="ORACLE_DWH",
        tgt_model="ORACLE_DWH", tgt_table="TGT_SYNTHETIC", update_key_columns=["ENTITY_ID"],
        source_columns=["ENTITY_ID", "ATTR_TEXT"],
        target_columns=["ENTITY_ID", "ATTR_TEXT", "INSERT_DATE"],
        source_system_literal="SYNTHETIC", source_table=source_table,
    )


def test_tgt_row_source_alias_keeps_the_creation_default_for_a_new_key(tmp_path):
    profile = _profile_for("TGT_ROW")
    alias = profile["lookup_alias"]
    assert alias != "TGT_ROW"
    report = _run_mapping_assignment(tmp_path, {
        "source_name": "TGT_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (11, "LOGICAL_A"),
        "integration": "IU",
        "target_attributes": ("ENTITY_ID", "SKEY", "INSERT_DATE"),
        "odi.expressions": f'SKEY=NVL(SRC.SKEY,-1);INSERT_DATE={profile["expressions"]["INSERT_DATE"]}',
        "odi.audit_preservation_columns": "INSERT_DATE",
        "odi.audit_lookup_alias": alias,
    })
    assert report["rejected"] is False, report["error_text"]
    assert "CASE" not in report["operator_output"] and "SELECT" not in report["operator_output"]
    assigned = report["assigned"]["INSERT_DATE"]
    assert assigned == profile["expressions"]["INSERT_DATE"].replace("SRC.", "TGT_ROW.")
    assert report["assigned"]["SKEY"] == "NVL(TGT_ROW.SKEY,-1)"
    assert _new_key_value(assigned, "TGT_ROW") != "OLD_AUDIT"


def test_emitted_alias_collision_refuses_before_assignment(tmp_path):
    profile = _profile_for("SRC_PLAIN")
    alias = profile["lookup_alias"]
    report = _run_mapping_assignment(tmp_path, {
        "source_name": alias,
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (11, "LOGICAL_A"),
        "integration": "IU",
        "odi.expressions": f'INSERT_DATE={profile["expressions"]["INSERT_DATE"]}',
        "odi.audit_preservation_columns": "INSERT_DATE",
        "odi.audit_lookup_alias": alias,
    })
    assert report["rejected"] is True
    assert report["error_class"] == "IllegalStateException"
    assert report["assigned"]["INSERT_DATE"] is None
    assert "CASE" not in report["error_text"] and "SELECT" not in report["error_text"]
    assert "CASE" not in report["operator_output"] and "SELECT" not in report["operator_output"]


def test_source_table_name_moves_the_lookup_alias_and_still_defaults_a_new_key(tmp_path):
    profile = _profile_for("ODI_AUDIT_TARGET_ROW")
    assert profile["lookup_alias"] == "ODI_AUDIT_TARGET_ROW_2"
    report = _run_mapping_assignment(tmp_path, {
        "source_name": "ODI_AUDIT_TARGET_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (11, "LOGICAL_A"),
        "integration": "IU",
        "odi.expressions": f'INSERT_DATE={profile["expressions"]["INSERT_DATE"]}',
        "odi.audit_preservation_columns": "INSERT_DATE",
        "odi.audit_lookup_alias": profile["lookup_alias"],
    })
    assert report["rejected"] is False, report["error_text"]
    assigned = report["assigned"]["INSERT_DATE"]
    assert _new_key_value(assigned, "ODI_AUDIT_TARGET_ROW") != "OLD_AUDIT"


def test_distinct_logical_schema_ids_reject_a_selected_profile(tmp_path):
    profile = _profile_for("SRC_ROW")
    report = _run_mapping_assignment(tmp_path, {
        "source_name": "SRC_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (22, "LOGICAL_B"),
        "integration": "IU",
        "target_attributes": ("ENTITY_ID", "SKEY", "INSERT_DATE"),
        "odi.expressions": f'SKEY=NVL(SRC.SKEY,-1);INSERT_DATE={profile["expressions"]["INSERT_DATE"]}',
        "odi.audit_preservation_columns": "INSERT_DATE",
        "odi.audit_lookup_alias": profile["lookup_alias"],
    })
    assert report["rejected"] is True
    assert report["error_class"] == "IllegalStateException"
    assert report["assigned"]["INSERT_DATE"] is None
    assert report["assigned"]["SKEY"] is None
    assert "CASE" not in report["error_text"] and "SELECT" not in report["error_text"]


def test_matching_logical_schema_ids_assign_the_exact_rewritten_text(tmp_path):
    profile = _profile_for("SRC_ROW")
    report = _run_mapping_assignment(tmp_path, {
        "source_name": "SRC_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (11, "LOGICAL_A"),
        "integration": "IU",
        "target_attributes": ("ENTITY_ID", "SKEY", "INSERT_DATE"),
        "odi.expressions": f'SKEY=NVL(SRC.SKEY,-1);INSERT_DATE={profile["expressions"]["INSERT_DATE"]}',
        "odi.audit_preservation_columns": "INSERT_DATE",
        "odi.audit_lookup_alias": profile["lookup_alias"],
    })
    assert report["rejected"] is False, report["error_text"]
    assert report["assigned"]["INSERT_DATE"] == profile["expressions"]["INSERT_DATE"].replace("SRC.", "SRC_ROW.")
    assert report["assigned"]["SKEY"] == "NVL(SRC_ROW.SKEY,-1)"
    assert "CASE" not in report["operator_output"] and "SELECT" not in report["operator_output"]
    assert "AUDIT PRESERVATION: applied count=1" in report["operator_output"]


def test_unqualified_mappings_without_a_profile_keep_the_timestamp_default(tmp_path):
    report = _run_mapping_assignment(tmp_path, {
        "source_name": "SRC_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (22, "LOGICAL_B"),
        "integration": "CONTROL_APPEND",
        "keys": (),
        "odi.expressions": None,
        "odi.audit_preservation_columns": None,
        "odi.audit_lookup_alias": None,
    })
    assert report["rejected"] is False, report["error_text"]
    assert report["assigned"]["INSERT_DATE"] == "CURRENT_TIMESTAMP"
    iu = _run_mapping_assignment(tmp_path, {
        "source_name": "SRC_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (22, "LOGICAL_B"),
        "integration": "IU",
        "odi.expressions": None,
        "odi.audit_preservation_columns": None,
        "odi.audit_lookup_alias": None,
    })
    assert iu["rejected"] is False, iu["error_text"]
    assert iu["assigned"]["INSERT_DATE"] == "CURRENT_TIMESTAMP"


_CASE_WORDS = {"CASE", "WHEN", "THEN", "ELSE", "END"}
_TIMESTAMP_DEFAULT = "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"
_LIVE_DEFAULTS = {
    "F1": {
        "INSERT_DATE": _TIMESTAMP_DEFAULT,
        "UPDATE_DATE": _TIMESTAMP_DEFAULT,
        "SOURCE_SYSTEM": f"CAST('{mapping_generator._SOURCE_SYSTEM_LITERAL}' AS VARCHAR2(50))",
        "ETL_BATCH_ID": f"TO_NUMBER({_SESSION_BATCH_SQL})",
    },
    "F3": {
        "INSERT_DATE": _TIMESTAMP_DEFAULT,
        "UPDATE_DATE": _TIMESTAMP_DEFAULT,
        "CURRENT_FLAG": "CAST('Y' AS VARCHAR2(1))",
        "EFFECTIVE_FROM": "TRUNC(CURRENT_TIMESTAMP)",
        "EFFECTIVE_TO": "TO_DATE('9999-12-31','YYYY-MM-DD')",
        "SOURCE_SYSTEM": f"CAST('{mapping_generator._SOURCE_SYSTEM_LITERAL}' AS VARCHAR2(50))",
        "ETL_BATCH_ID": f"TO_NUMBER({_SESSION_BATCH_SQL})",
    },
}


def _top_level_keywords(expression: str) -> list[tuple[str, int, int]]:
    """CASE keywords that are outside parentheses and string literals."""
    found: list[tuple[str, int, int]] = []
    depth = 0
    in_string = False
    index = 0
    length = len(expression)
    while index < length:
        char = expression[index]
        if in_string:
            if char == "'" and index + 1 < length and expression[index + 1] == "'":
                index += 2
                continue
            if char == "'":
                in_string = False
            index += 1
            continue
        if char == "'":
            in_string = True
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char.isalpha():
            end = index + 1
            while end < length and (expression[end].isalnum() or expression[end] == "_"):
                end += 1
            word = expression[index:end].upper()
            if word in _CASE_WORDS:
                found.append((word, index, end))
            index = end
            continue
        index += 1
    return found


def _case_results(expression: str) -> list[str]:
    """THEN and ELSE results of the outer CASE. Nested change-detection CASE stays inside."""
    tokens = _top_level_keywords(expression)
    kinds = [kind for kind, _start, _end in tokens]
    assert kinds[0] == "CASE" and kinds[-1] == "END" and kinds.count("CASE") == 1
    assert "ELSE" in kinds and "THEN" in kinds
    results = []
    for position, (kind, _start, end) in enumerate(tokens):
        if kind in {"THEN", "ELSE"}:
            results.append(expression[end:tokens[position + 1][1]].strip())
    assert len(results) >= 3
    return results


def _branch_sql_type(branch: str, column: str) -> str:
    text = branch.strip()
    if text.startswith("(SELECT "):
        select_list = text.split(" FROM ", 1)[0]
        assert f'."{column}"' in select_list and "TO_NUMBER" not in select_list
        return "COLUMN"
    if re.fullmatch(r"TO_NUMBER\(.+\)", text):
        return "NUMBER"
    cast = re.fullmatch(r"CAST\(.+ AS ([A-Z0-9_(), ]+)\)", text)
    if cast:
        return cast.group(1)
    if text == "CURRENT_TIMESTAMP":
        return "TIMESTAMP WITH TIME ZONE"
    if re.fullmatch(r"TO_TIMESTAMP\(.+\)", text):
        return "TIMESTAMP"
    if text.startswith(("TRUNC(", "TO_DATE(")):
        return "DATE"
    if re.fullmatch(r"'(?:''|[^'])*'", text):
        return "CHAR"
    raise AssertionError(f"unclassified branch: {text}")


def _comparable_type(text: str) -> str:
    """TIMESTAMP(6) is the default precision, so it compares equal to bare TIMESTAMP."""
    return text.replace("TIMESTAMP(6)", "TIMESTAMP")


def _assert_branches_match(expression: str, column: str, oracle_type: str, default_sql: str | None = None) -> None:
    """Every CASE result has the declared type. The ambiguous guard is a WHERE predicate."""
    results = _case_results(expression)
    ambiguous, *middle, matched = results
    where_at = ambiguous.upper().find(" WHERE ")
    guard_at = ambiguous.find("AND TO_NUMBER('AMBIGUOUS') IS NOT NULL)")
    assert ambiguous.startswith("(SELECT ") and where_at != -1 and guard_at > where_at
    assert "TO_NUMBER('AMBIGUOUS')" not in ambiguous.split(" FROM ", 1)[0]
    assert "TO_NUMBER('AMBIGUOUS')" not in matched
    assert all(not result.strip().startswith("TO_NUMBER('AMBIGUOUS')") for result in results)
    assert middle and len(set(middle)) == 1
    if default_sql is not None:
        assert middle[0] == default_sql
    for result in results:
        found = _branch_sql_type(result, column)
        if found == "COLUMN":
            found = oracle_type
        assert _comparable_type(found) == _comparable_type(oracle_type), (column, oracle_type, found, result)


def _typed_expression(column: str, type_sql: str) -> str:
    profile = build_same_schema_audit_expressions(
        integration_type="IU",
        src_owner="DWH",
        tgt_owner="DWH",
        src_model="ORACLE_DWH",
        tgt_model="ORACLE_ODS",
        tgt_table="ODS_ODI81N_F1",
        update_key_columns=["ENTITY_ID"],
        source_columns=["ENTITY_ID", "ATTR_TEXT"],
        target_columns=["ENTITY_ID", "ATTR_TEXT", column],
        source_system_literal=mapping_generator._SOURCE_SYSTEM_LITERAL,
        target_column_types={column: type_sql},
    )
    assert profile["qualified"] is True
    return str(profile["expressions"][column])


@pytest.mark.parametrize("flow", ["F1", "F3"])
def test_live_audit_case_branches_match_the_target_datatype(flow):
    """a645d5a fails here: NUMBER/TIMESTAMP/VARCHAR2 defaults were CHAR or TIMESTAMP WITH TIME ZONE."""
    _generator_unused, _validated, plan = _generator(flow)
    types = {name: _canonical_audit_type(type_sql) for name, type_sql in plan["desired_col_defs"]}
    expressions = _profile(flow)["expressions"]
    assert set(expressions) == set(_LIVE_DEFAULTS[flow])
    for column, default_sql in _LIVE_DEFAULTS[flow].items():
        expression = expressions[column]
        _assert_branches_match(expression, column, types[column], default_sql)
        if column == "ETL_BATCH_ID":
            assert f"THEN {_SESSION_BATCH_SQL}" not in expression
        if column in {"INSERT_DATE", "UPDATE_DATE"}:
            assert "THEN CURRENT_TIMESTAMP " not in expression
        if column == "SOURCE_SYSTEM":
            assert f"THEN '{mapping_generator._SOURCE_SYSTEM_LITERAL}'" not in expression
    del _generator_unused


@pytest.mark.parametrize("type_sql", ["CLOB", "RAW(16)", "INTEGER", "TIMESTAMP WITH TIMEZONE", ""])
def test_unsupported_audit_column_type_is_rejected_without_echoing_it(type_sql):
    with pytest.raises(
        AuditPreservationRejected, match=r"^Audit preservation column type is not supported\.$",
    ) as caught:
        _typed_expression("UPDATE_DATE", type_sql)
    if type_sql:
        assert type_sql.upper() not in str(caught.value)


def test_missing_audit_column_type_is_rejected():
    with pytest.raises(AuditPreservationRejected, match=r"^Audit preservation column type is missing\.$"):
        build_same_schema_audit_expressions(
            integration_type="IU", src_owner="DWH", tgt_owner="DWH", src_model="ORACLE_DWH",
            tgt_model="ORACLE_ODS", tgt_table="ODS_ODI81N_F1", update_key_columns=["ENTITY_ID"],
            source_columns=["ENTITY_ID"], target_columns=["ENTITY_ID", "ETL_BATCH_ID"],
            source_system_literal="ORACLE_SAMPLE_SCHEMAS", target_column_types={},
        )


def test_empty_source_columns_name_only_the_source_model():
    generator = MappingGenerator()
    generator._source_columns_for = lambda _params: []
    params = driver.classic_params("F1")
    params["src_model"] = "ORACLE_SOURCE"
    params["src_table"] = "DWH.ODI81N_SRC_F1"
    with pytest.raises(
        SourceTableNotFound,
        match=r"^Source table not found through source model ORACLE_SOURCE$",
    ) as caught:
        generator.target_table_preflight(params)
    assert "ODI81N_SRC_F1" not in str(caught.value)
    assert caught.value.src_model == "ORACLE_SOURCE"
    unknown = SourceTableNotFound("not a model")
    assert str(unknown) == "Source table not found through source model UNKNOWN"
    assert "not a model" not in str(unknown)


def _default_sql(column: str, type_sql: str) -> str:
    _ambiguous, *middle, _matched = _case_results(_typed_expression(column, type_sql))
    assert middle and len(set(middle)) == 1
    return middle[0]


@pytest.mark.parametrize(("column", "type_sql", "expected"), [
    ("INSERT_DATE", "DATE", "CAST(CURRENT_TIMESTAMP AS DATE)"),
    ("INSERT_DATE", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
    ("INSERT_DATE", "TIMESTAMP(6)", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6))"),
    ("INSERT_DATE", "TIMESTAMP(3)", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3))"),
    ("INSERT_DATE", "TIMESTAMP WITH TIME ZONE", "CURRENT_TIMESTAMP"),
    ("INSERT_DATE", "TIMESTAMP(6) WITH TIME ZONE", "CURRENT_TIMESTAMP"),
    ("INSERT_DATE", "TIMESTAMP(3) WITH TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3) WITH TIME ZONE)"),
    ("INSERT_DATE", "TIMESTAMP WITH LOCAL TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP WITH LOCAL TIME ZONE)"),
    ("INSERT_DATE", "TIMESTAMP(6) WITH LOCAL TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6) WITH LOCAL TIME ZONE)"),
    ("INSERT_DATE", "TIMESTAMP(9) WITH LOCAL TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(9) WITH LOCAL TIME ZONE)"),
    ("UPDATE_DATE", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
    ("EFFECTIVE_FROM", "DATE", "TRUNC(CURRENT_TIMESTAMP)"),
    ("EFFECTIVE_FROM", "TIMESTAMP(6)", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(6))"),
    ("EFFECTIVE_FROM", "TIMESTAMP(6) WITH TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(6) WITH TIME ZONE)"),
    ("EFFECTIVE_FROM", "TIMESTAMP(6) WITH LOCAL TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(6) WITH LOCAL TIME ZONE)"),
    ("EFFECTIVE_TO", "DATE", "TO_DATE('9999-12-31','YYYY-MM-DD')"),
    ("EFFECTIVE_TO", "TIMESTAMP(6)", "CAST(TO_DATE('9999-12-31','YYYY-MM-DD') AS TIMESTAMP(6))"),
    ("EFFECTIVE_TO", "TIMESTAMP WITH TIME ZONE", "CAST(TO_DATE('9999-12-31','YYYY-MM-DD') AS TIMESTAMP WITH TIME ZONE)"),
    ("EFFECTIVE_TO", "TIMESTAMP WITH LOCAL TIME ZONE", "CAST(TO_DATE('9999-12-31','YYYY-MM-DD') AS TIMESTAMP WITH LOCAL TIME ZONE)"),
])
def test_datetime_defaults_use_plain_oracle_for_the_declared_type(column, type_sql, expected):
    """4936390 emitted TO_CHAR/TO_TIMESTAMP for a TIMESTAMP target. Oracle rejects that."""
    assert _default_sql(column, type_sql) == expected


_SOURCE_LITERAL = f"'{mapping_generator._SOURCE_SYSTEM_LITERAL}'"
_AUDIT_PRESERVATION_SENTENCE = (
    "The target table's audit column types are not supported for audit preservation. "
    "Nothing was created."
)
_CHARACTER_DECLARED = (
    ("CHAR(1)", "CHAR(1)"),
    ("CHAR(1 BYTE)", "CHAR(1 BYTE)"),
    ("CHAR(1 CHAR)", "CHAR(1 CHAR)"),
    ("VARCHAR2(1)", "VARCHAR2(1)"),
    ("VARCHAR2(20)", "VARCHAR2(20)"),
    ("VARCHAR2(50)", "VARCHAR2(50)"),
    ("VARCHAR2(50 BYTE)", "VARCHAR2(50 BYTE)"),
    ("VARCHAR2(50 CHAR)", "VARCHAR2(50 CHAR)"),
    ("NVARCHAR2(1)", "NVARCHAR2(1)"),
    ("NVARCHAR2(20)", "NVARCHAR2(20)"),
    ("NCHAR(1)", "NCHAR(1)"),
    ("VARCHAR2(50) DEFAULT 'ORACLE_SAMPLE_SCHEMAS'", "VARCHAR2(50)"),
)
_INSERT_UPDATE_TEXT = (
    ("DATE", "CAST(CURRENT_TIMESTAMP AS DATE)"),
    ("TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
    ("TIMESTAMP(6)", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6))"),
    ("TIMESTAMP(3)", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3))"),
    ("TIMESTAMP WITH TIME ZONE", "CURRENT_TIMESTAMP"),
    ("TIMESTAMP(6) WITH TIME ZONE", "CURRENT_TIMESTAMP"),
    ("TIMESTAMP(3) WITH TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3) WITH TIME ZONE)"),
    ("TIMESTAMP WITH LOCAL TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP WITH LOCAL TIME ZONE)"),
    ("TIMESTAMP(6) WITH LOCAL TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6) WITH LOCAL TIME ZONE)"),
    ("TIMESTAMP(9) WITH LOCAL TIME ZONE", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP(9) WITH LOCAL TIME ZONE)"),
    ("TIMESTAMP DEFAULT SYSTIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
)
_EFFECTIVE_FROM_TEXT = (
    ("DATE", "TRUNC(CURRENT_TIMESTAMP)"),
    ("DATE DEFAULT TRUNC(SYSDATE)", "TRUNC(CURRENT_TIMESTAMP)"),
    ("TIMESTAMP", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP)"),
    ("TIMESTAMP(6)", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(6))"),
    ("TIMESTAMP(3)", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(3))"),
    ("TIMESTAMP WITH TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP WITH TIME ZONE)"),
    ("TIMESTAMP(6) WITH TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(6) WITH TIME ZONE)"),
    ("TIMESTAMP(3) WITH TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(3) WITH TIME ZONE)"),
    ("TIMESTAMP WITH LOCAL TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP WITH LOCAL TIME ZONE)"),
    ("TIMESTAMP(6) WITH LOCAL TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(6) WITH LOCAL TIME ZONE)"),
    ("TIMESTAMP(9) WITH LOCAL TIME ZONE", "CAST(TRUNC(CURRENT_TIMESTAMP) AS TIMESTAMP(9) WITH LOCAL TIME ZONE)"),
)
_EFFECTIVE_TO_EXPR = "TO_DATE('9999-12-31','YYYY-MM-DD')"
_EFFECTIVE_TO_TEXT = (
    ("DATE", _EFFECTIVE_TO_EXPR),
    ("DATE DEFAULT TO_DATE('9999-12-31','YYYY-MM-DD')", _EFFECTIVE_TO_EXPR),
    ("TIMESTAMP", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP)"),
    ("TIMESTAMP(6)", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP(6))"),
    ("TIMESTAMP(3)", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP(3))"),
    ("TIMESTAMP WITH TIME ZONE", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP WITH TIME ZONE)"),
    ("TIMESTAMP(6) WITH TIME ZONE", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP(6) WITH TIME ZONE)"),
    ("TIMESTAMP(3) WITH TIME ZONE", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP(3) WITH TIME ZONE)"),
    ("TIMESTAMP WITH LOCAL TIME ZONE", f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP WITH LOCAL TIME ZONE)"),
    (
        "TIMESTAMP(6) WITH LOCAL TIME ZONE",
        f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP(6) WITH LOCAL TIME ZONE)",
    ),
    (
        "TIMESTAMP(9) WITH LOCAL TIME ZONE",
        f"CAST({_EFFECTIVE_TO_EXPR} AS TIMESTAMP(9) WITH LOCAL TIME ZONE)",
    ),
)
_BATCH_NUMBER_TEXT = (
    ("NUMBER", f"TO_NUMBER({_SESSION_BATCH_SQL})"),
    ("NUMBER NOT NULL", f"TO_NUMBER({_SESSION_BATCH_SQL})"),
    ("NUMBER(19)", f"CAST(TO_NUMBER({_SESSION_BATCH_SQL}) AS NUMBER(19))"),
    ("NUMBER(19, 0)", f"CAST(TO_NUMBER({_SESSION_BATCH_SQL}) AS NUMBER(19,0))"),
    ("FLOAT", f"CAST(TO_NUMBER({_SESSION_BATCH_SQL}) AS FLOAT)"),
    ("FLOAT(126)", f"CAST(TO_NUMBER({_SESSION_BATCH_SQL}) AS FLOAT(126))"),
    ("BINARY_DOUBLE", f"CAST(TO_NUMBER({_SESSION_BATCH_SQL}) AS BINARY_DOUBLE)"),
    ("BINARY_FLOAT", f"CAST(TO_NUMBER({_SESSION_BATCH_SQL}) AS BINARY_FLOAT)"),
)


def _supported_audit_pairs() -> list[tuple[str, str, str]]:
    """Every supported audit column and declared type, with the exact default text."""
    rows: list[tuple[str, str, str]] = []
    for column in ("INSERT_DATE", "UPDATE_DATE"):
        rows.extend((column, type_sql, expected) for type_sql, expected in _INSERT_UPDATE_TEXT)
    rows.extend(("EFFECTIVE_FROM", type_sql, expected) for type_sql, expected in _EFFECTIVE_FROM_TEXT)
    rows.extend(("EFFECTIVE_TO", type_sql, expected) for type_sql, expected in _EFFECTIVE_TO_TEXT)
    literals = {
        "CURRENT_FLAG": "'Y'",
        "SOURCE_SYSTEM": _SOURCE_LITERAL,
        "ETL_BATCH_ID": _SESSION_BATCH_SQL,
    }
    for column, expression in literals.items():
        for declared, canonical in _CHARACTER_DECLARED:
            rows.append((column, declared, f"CAST({expression} AS {canonical})"))
    rows.extend(("ETL_BATCH_ID", type_sql, expected) for type_sql, expected in _BATCH_NUMBER_TEXT)
    return rows


@pytest.mark.parametrize(("column", "type_sql", "expected"), _supported_audit_pairs())
def test_supported_audit_pair_emits_the_closed_table_text(column, type_sql, expected):
    """Every branch matches the declared type. An NCHAR matched branch of numeric 0 fails this."""
    expression = _typed_expression(column, type_sql)
    _ambiguous, *middle, _matched = _case_results(expression)
    assert middle and len(set(middle)) == 1 and middle[0] == expected
    _assert_branches_match(expression, column, _canonical_audit_type(type_sql), expected)


def test_etl_batch_id_varchar2_20_casts_the_session_token():
    """d2891fb counted the unsubstituted session token and rejected VARCHAR2(20)."""
    assert _default_sql("ETL_BATCH_ID", "VARCHAR2(20)") == f"CAST({_SESSION_BATCH_SQL} AS VARCHAR2(20))"


def _columns_with(column: str, type_sql: str) -> list[dict]:
    return [
        {"COLUMN_NAME": "ENTITY_ID", "ORACLE_TYPE": "NUMBER(10)", "IS_NULLABLE": "N"},
        {"COLUMN_NAME": "ATTR_TEXT", "ORACLE_TYPE": "VARCHAR2(40)", "IS_NULLABLE": "Y"},
        {"COLUMN_NAME": column, "ORACLE_TYPE": type_sql, "IS_NULLABLE": "Y"},
    ]


@pytest.mark.parametrize(("column", "type_sql"), [
    ("CURRENT_FLAG", "DATE"),
    ("SOURCE_SYSTEM", "TIMESTAMP"),
    ("INSERT_DATE", "VARCHAR2(30)"),
    ("CURRENT_FLAG", "NUMBER(1)"),
    ("ETL_BATCH_ID", "DATE"),
])
def test_unsupported_audit_pair_is_rejected_before_any_ddl(column, type_sql, monkeypatch):
    import odi_orchestrator
    import target_table_service

    ddl: list[bool] = []
    constructed: list[bool] = []
    monkeypatch.setattr(
        target_table_service, "execute_target_table_plan", lambda *_args, **_kwargs: ddl.append(True),
    )
    real_init = odi_orchestrator.OdiOrchestrator.__init__

    def spy_init(self, *args, **kwargs):
        constructed.append(True)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "__init__", spy_init)
    generator = MappingGenerator()
    generator._source_columns_for = lambda _params: _columns_with(column, type_sql)
    validated = generator.validate_params(driver.classic_params("F1"))
    with pytest.raises(
        AuditPreservationRejected, match=r"^Audit preservation column type is not supported\.$",
    ):
        generator._create_target_table(validated)
    assert ddl == []
    assert constructed == []


_F1_DECLARED = {
    "INSERT_DATE": "TIMESTAMP",
    "UPDATE_DATE": "TIMESTAMP",
    "SOURCE_SYSTEM": "VARCHAR2(50)",
    "ETL_BATCH_ID": "NUMBER",
}


def _declared_frame(types: dict[str, str]):
    import pandas as pd

    return pd.DataFrame([
        {
            "COLUMN_NAME": name,
            "DATA_TYPE": value.split("(", 1)[0],
            "DATA_LENGTH": None,
            "DATA_PRECISION": None,
            "DATA_SCALE": None,
            "NULLABLE": "Y",
            "FULL_DATA_TYPE": value,
            "COMMENTS": None,
        }
        for name, value in types.items()
    ])


def _create_with_catalog(monkeypatch, source, *, exists, declared=None, drop=False, read_error=False):
    """Build one classic target with a stubbed catalog. The DDL function only records a call."""
    import target_table_service
    from exceptions import MappingGenerationError

    generator = MappingGenerator()
    generator._source_columns_for = lambda _params: source
    params = generator.validate_params(driver.classic_params("F1"))
    if drop:
        params["drop_target_if_exists"] = True
    reads: list[tuple[str, str]] = []
    ddl: list[bool] = []

    def read_schema(owner, table):
        reads.append((owner, table))
        if read_error:
            raise MappingGenerationError("Target table column type read failed")
        return _declared_frame(declared or {})

    monkeypatch.setattr(target_table_service, "_target_exists", lambda _context: exists)
    monkeypatch.setattr(target_table_service, "read_table_schema", read_schema)
    monkeypatch.setattr(
        target_table_service, "execute_target_table_plan",
        lambda *_args, **_kwargs: ddl.append(True) or {
            "status": "CREATED" if drop or not exists else "ALREADY_IN_SYNC",
            "ddl": "",
        },
    )
    return generator, params, reads, ddl


def test_unsupported_pair_is_rejected_before_repository_preflight(app_client, monkeypatch):
    """a8d9b76 ran repository preflight first, so its failure hid this rejection."""
    import odi_orchestrator
    import target_table_service
    from mapping_generator import RepositoryPreflightBlockedError

    calls: list[str] = []
    ddl: list[bool] = []
    constructed: list[bool] = []

    def explode(self, _params):
        calls.append("preflight")
        raise RepositoryPreflightBlockedError({"code": "repository_lookup_failed"})

    real_init = odi_orchestrator.OdiOrchestrator.__init__

    def spy_init(self, *args, **kwargs):
        constructed.append(True)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(MappingGenerator, "_preflight_odi_repository", explode)
    monkeypatch.setattr(
        MappingGenerator, "_source_columns_for",
        lambda self, _params: _columns_with("CURRENT_FLAG", "NUMBER(1)"),
    )
    monkeypatch.setattr(MappingGenerator, "_log_to_mapping_log", lambda self, *_args, **_kwargs: None)
    monkeypatch.setattr(
        target_table_service, "execute_target_table_plan", lambda *_args, **_kwargs: ddl.append(True),
    )
    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "__init__", spy_init)
    response = app_client.post("/api/mapping/generate", json=driver.classic_generate_body("F1"))
    body = response.get_json()
    assert response.status_code == 200
    assert body["overall_status"] == "FAILED"
    assert body["error_code"] == "audit_preservation_rejected"
    assert body["error"] == _AUDIT_PRESERVATION_SENTENCE
    assert calls == []
    assert ddl == []
    assert constructed == []
    assert "create_target_table" not in body["steps"]
    assert "create_mapping" not in body["steps"]


def test_reused_number_target_types_every_branch_as_number(monkeypatch):
    """a8d9b76 cast the session token to the planned VARCHAR2 against a NUMBER column."""
    generator, params, reads, ddl = _create_with_catalog(
        monkeypatch,
        _columns_with("ETL_BATCH_ID", "VARCHAR2(20)"),
        exists=True,
        declared={**_F1_DECLARED, "ETL_BATCH_ID": "NUMBER"},
    )
    outcome = generator._create_target_table(params)
    expected = f"TO_NUMBER({_SESSION_BATCH_SQL})"
    _assert_branches_match(
        generator._audit_preservation_expressions["ETL_BATCH_ID"], "ETL_BATCH_ID", "NUMBER", expected,
    )
    assert outcome["status"] == "ALREADY_IN_SYNC"
    assert reads == [("DWH", "ODS_ODI81N_F1")]
    assert ddl == [True]


def test_reused_varchar2_target_casts_the_session_token(monkeypatch):
    """a8d9b76 used TO_NUMBER for a plan of NUMBER against an existing VARCHAR2(20)."""
    generator, params, reads, ddl = _create_with_catalog(
        monkeypatch,
        [dict(column) for column in _SOURCE_COLUMNS],
        exists=True,
        declared={**_F1_DECLARED, "ETL_BATCH_ID": "VARCHAR2(20)"},
    )
    generator._create_target_table(params)
    expected = f"CAST({_SESSION_BATCH_SQL} AS VARCHAR2(20))"
    _assert_branches_match(
        generator._audit_preservation_expressions["ETL_BATCH_ID"],
        "ETL_BATCH_ID", "VARCHAR2(20)", expected,
    )
    assert reads == [("DWH", "ODS_ODI81N_F1")]
    assert ddl == [True]


def test_created_or_recreated_target_keeps_the_planned_audit_text(monkeypatch):
    """A table this request creates uses the planned type, including after a drop."""
    expected = f"CAST({_SESSION_BATCH_SQL} AS VARCHAR2(20))"
    for exists, drop in ((False, False), (True, True)):
        generator, params, reads, _ddl = _create_with_catalog(
            monkeypatch,
            _columns_with("ETL_BATCH_ID", "VARCHAR2(20)"),
            exists=exists,
            declared={**_F1_DECLARED, "ETL_BATCH_ID": "NUMBER"},
            drop=drop,
        )
        generator._create_target_table(params)
        assert reads == []
        _assert_branches_match(
            generator._audit_preservation_expressions["ETL_BATCH_ID"],
            "ETL_BATCH_ID", "VARCHAR2(20)", expected,
        )
    number_plan, params, reads, _ddl = _create_with_catalog(
        monkeypatch, [dict(column) for column in _SOURCE_COLUMNS], exists=False,
    )
    number_plan._create_target_table(params)
    assert reads == []
    _assert_branches_match(
        number_plan._audit_preservation_expressions["ETL_BATCH_ID"],
        "ETL_BATCH_ID", "NUMBER", f"TO_NUMBER({_SESSION_BATCH_SQL})",
    )


def test_reused_unsupported_actual_type_stops_before_ddl_and_create(monkeypatch):
    """a8d9b76 accepted a reused DATE audit column because the plan said NUMBER."""
    import odi_orchestrator
    import target_table_service

    preflight: list[bool] = []
    ddl: list[bool] = []
    created: list[str] = []
    constructed: list[bool] = []
    real_init = odi_orchestrator.OdiOrchestrator.__init__

    def spy_init(self, *args, **kwargs):
        constructed.append(True)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(
        MappingGenerator, "_preflight_odi_repository",
        lambda self, _params: preflight.append(True) or {"status": "passed", "ikm_id": "7"},
    )
    monkeypatch.setattr(
        MappingGenerator, "_resolve_odi_repository_bindings",
        lambda self, _params: {"status": "passed"},
    )
    monkeypatch.setattr(
        MappingGenerator, "_source_columns_for",
        lambda self, _params: [dict(column) for column in _SOURCE_COLUMNS],
    )
    monkeypatch.setattr(
        MappingGenerator, "_register_odi_datastores", lambda self, _params: created.append("register"),
    )
    monkeypatch.setattr(
        MappingGenerator, "_create_odi_mapping", lambda self, _params: created.append("create"),
    )
    monkeypatch.setattr(MappingGenerator, "_log_to_mapping_log", lambda self, *_args, **_kwargs: None)
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _context: True)
    monkeypatch.setattr(
        target_table_service, "read_table_schema",
        lambda _owner, _table: _declared_frame({**_F1_DECLARED, "ETL_BATCH_ID": "DATE"}),
    )
    monkeypatch.setattr(
        target_table_service, "execute_target_table_plan",
        lambda *_args, **_kwargs: ddl.append(True) or {"status": "ALREADY_IN_SYNC", "ddl": ""},
    )
    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "__init__", spy_init)
    result = MappingGenerator().generate_from_params(driver.classic_params("F1"))
    assert result["overall_status"] == "FAILED"
    assert result.get("error_code") == "audit_preservation_rejected"
    assert preflight == [True]
    assert ddl == []
    assert created == []
    assert constructed == []
    assert "create_target_table" not in result["steps"]
    assert "create_mapping" not in result["steps"]


def test_reused_target_type_read_failure_is_rejected(monkeypatch):
    """a8d9b76 kept the planned type when the existing target's types could not be read."""
    import target_table_service
    from exceptions import MappingGenerationError

    ddl: list[bool] = []
    created: list[str] = []
    monkeypatch.setattr(
        MappingGenerator, "_preflight_odi_repository",
        lambda self, _params: {"status": "passed", "ikm_id": "7"},
    )
    monkeypatch.setattr(
        MappingGenerator, "_resolve_odi_repository_bindings",
        lambda self, _params: {"status": "passed"},
    )
    monkeypatch.setattr(
        MappingGenerator, "_source_columns_for",
        lambda self, _params: [dict(column) for column in _SOURCE_COLUMNS],
    )
    monkeypatch.setattr(
        MappingGenerator, "_register_odi_datastores", lambda self, _params: created.append("register"),
    )
    monkeypatch.setattr(
        MappingGenerator, "_create_odi_mapping", lambda self, _params: created.append("create"),
    )
    monkeypatch.setattr(MappingGenerator, "_log_to_mapping_log", lambda self, *_args, **_kwargs: None)
    monkeypatch.setattr(target_table_service, "_target_exists", lambda _context: True)

    def fail(_owner, _table):
        raise MappingGenerationError("Target table column type read failed")

    monkeypatch.setattr(target_table_service, "read_table_schema", fail)
    monkeypatch.setattr(
        target_table_service, "execute_target_table_plan",
        lambda *_args, **_kwargs: ddl.append(True) or {"status": "ALREADY_IN_SYNC", "ddl": ""},
    )
    result = MappingGenerator().generate_from_params(driver.classic_params("F1"))
    assert result["overall_status"] == "FAILED"
    assert result.get("error_code") == "audit_preservation_rejected"
    assert result["error"] == "Audit preservation target column types could not be read."
    assert "Target table column type read failed" not in result["error"]
    assert ddl == []
    assert created == []
    assert "create_mapping" not in result["steps"]


def test_audit_preservation_rejection_reaches_the_wizard_as_a_fixed_200(app_client, monkeypatch):
    """Classic generate names this rejection and does not create a table or a mapping."""
    import odi_orchestrator
    import target_table_service

    ddl: list[bool] = []
    constructed: list[bool] = []
    logged: list[tuple] = []
    snapshots: list[bool] = []
    monkeypatch.setattr(
        target_table_service, "execute_target_table_plan", lambda *_args, **_kwargs: ddl.append(True),
    )
    real_init = odi_orchestrator.OdiOrchestrator.__init__

    def spy_init(self, *args, **kwargs):
        constructed.append(True)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "__init__", spy_init)
    monkeypatch.setattr(
        MappingGenerator, "_source_columns_for",
        lambda self, _params: _columns_with("CURRENT_FLAG", "NUMBER(1)"),
    )
    monkeypatch.setattr(
        MappingGenerator, "_preflight_odi_repository",
        lambda self, _params: {"status": "passed", "side_effects": False, "ikm_id": "7"},
    )
    monkeypatch.setattr(
        MappingGenerator, "_resolve_odi_repository_bindings",
        lambda self, _params: {"status": "passed", "mapping_exists": False},
    )
    monkeypatch.setattr(
        MappingGenerator, "_log_to_mapping_log",
        lambda self, *args, **_kwargs: logged.append(args),
    )
    monkeypatch.setattr(
        MappingGenerator, "_take_snapshot",
        lambda self, *_args, **_kwargs: snapshots.append(True),
    )
    response = app_client.post("/api/mapping/generate", json=driver.classic_generate_body("F1"))
    assert response.status_code == 200
    body = response.get_json()
    assert body["overall_status"] == "FAILED"
    assert body["error_code"] == "audit_preservation_rejected"
    assert body["error"] == _AUDIT_PRESERVATION_SENTENCE
    assert body["error_type"] == "OperationError"
    text = response.get_data(as_text=True)
    assert "Audit preservation" not in text
    assert "session number" not in text
    assert "create_target_table" not in body["steps"]
    assert "create_mapping" not in body["steps"]
    assert ddl == []
    assert constructed == []
    assert snapshots == []
    assert logged and logged[0][1] == "FAILED"


_QUOTED_BATCH = "'<%=odiRef.getSession(\"SESS_NO\")%>'"


def _fallback_spec(attribute: object) -> dict:
    return {
        "source_name": "SRC_ROW",
        "src_schema": (11, "LOGICAL_A"),
        "tgt_schema": (22, "LOGICAL_B"),
        "integration": "IU",
        "source_attributes": ("ENTITY_ID",),
        "target_attributes": (attribute,),
        "odi.expressions": None,
        "odi.audit_preservation_columns": None,
        "odi.audit_lookup_alias": None,
    }


def test_graph_fallback_casts_a_number_session_batch(tmp_path):
    """4936390 always quoted the session token, including on a NUMBER column."""
    report = _run_mapping_assignment(tmp_path, _fallback_spec(
        {"name": "ETL_BATCH_ID", "code": "NUMBER", "length": 19, "scale": 0},
    ))
    assert report["rejected"] is False, report["error_text"]
    assert report["assigned"]["ETL_BATCH_ID"] == f"CAST(TO_NUMBER({_QUOTED_BATCH}) AS NUMBER(19,0))"
    bare = _run_mapping_assignment(tmp_path, _fallback_spec(
        {"name": "ETL_BATCH_ID", "code": "NUMBER"},
    ))
    assert bare["assigned"]["ETL_BATCH_ID"] == f"TO_NUMBER({_QUOTED_BATCH})"
    floating = _run_mapping_assignment(tmp_path, _fallback_spec(
        {"name": "ETL_BATCH_ID", "code": "BINARY_DOUBLE"},
    ))
    assert floating["assigned"]["ETL_BATCH_ID"] == f"CAST(TO_NUMBER({_QUOTED_BATCH}) AS BINARY_DOUBLE)"


def test_graph_fallback_keeps_the_quoted_session_literal_without_a_datatype(tmp_path):
    report = _run_mapping_assignment(tmp_path, _fallback_spec("ETL_BATCH_ID"))
    assert report["rejected"] is False, report["error_text"]
    assert report["assigned"]["ETL_BATCH_ID"] == _QUOTED_BATCH
    dated = _run_mapping_assignment(tmp_path, _fallback_spec("INSERT_DATE"))
    assert dated["assigned"]["INSERT_DATE"] == "CURRENT_TIMESTAMP"


def test_session_batch_fallback_calls_the_typed_helper():
    text = _mapping_script()
    assert text.count("tAttr.setExpressionText(typedSessionBatch(tAttr))") == 2
    assert "setExpressionText(\"'<%=odiRef.getSession" not in text
