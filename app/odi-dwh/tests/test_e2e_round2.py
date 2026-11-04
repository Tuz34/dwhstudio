"""Behavioral regressions for PR 16. All data and external boundaries are synthetic."""

from unittest.mock import Mock

import pytest

import mapping_generator as mg
import odi_orchestrator as oo
import target_table_service as ts
from ikm_policy import resolve_approved_ikm


APPROVED = "IKM Oracle Merge,IKM Oracle Insert,IKM Oracle Incremental Update (MERGE)"
BASE = {
    "mapping_name": "MAP_TEST", "src_model": "ORACLE_HR", "src_table": "HR.TEST",
    "tgt_model": "ORACLE_ODS", "tgt_owner": "DWH", "tgt_table": "ODS_TEST",
}
PLAN = {
    "src_columns": [{"COLUMN_NAME": "ID"}], "desired_col_defs": [("ID", "NUMBER"), ("NAME", "VARCHAR2(10)")],
    "col_defs": ['"ID" NUMBER', '"NAME" VARCHAR2(10)'], "defaults_added": [], "unique_key": ["ID"],
}
KNOWN = (
    "Nov 04, 2026 8:36:35 PM oracle.as.jmx.framework.PortableMBeanFactory <init>\n"
    "SEVERE: Error initializing the JMX FRamework SPI\n"
    "java.lang.NoClassDefFoundError: javax/management/j2ee/statistics/Stats\n"
    "\tat java.lang.Class.forName(Class.java:348)\n"
    "Caused by: java.lang.ClassNotFoundException: javax.management.j2ee.statistics.Stats\n"
    "\tat java.net.URLClassLoader.findClass(URLClassLoader.java:382)\n"
    "\t... 12 more"
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(mg, "SOURCE_ORACLE_SCHEMAS", ["HR"])
    for module in (mg, oo):
        monkeypatch.setattr(module, "ODI_APPROVED_IKMS", APPROVED)
        monkeypatch.setattr(module, "ODI_IKM_PRIORITY", "oracle merge,oracle insert")
    monkeypatch.setattr(ts, "_connection", Mock(side_effect=AssertionError("unmocked DB call")))
    monkeypatch.setattr(mg.MappingGenerator, "_log_to_mapping_log", lambda *a, **kw: None)


@pytest.mark.parametrize("extra", [
    {}, {"ikm_name": "IKM Oracle Merge"},
    {"tgt_table": "SKEY_TEST", "needs_skey": True, "skey_natural_key": "ID", "ikm_name": "IKM Oracle Insert"},
])
def test_keyless_effective_merge_stops_before_any_ddl_or_odi(extra, monkeypatch):
    generator = mg.MappingGenerator()
    ddl = Mock(side_effect=AssertionError("DDL reached"))
    odi = Mock(side_effect=AssertionError("ODI reached"))
    monkeypatch.setattr(generator, "_create_target_table", ddl)
    monkeypatch.setattr(generator, "_resolve_odi_repository_bindings", odi)
    result = generator.generate_from_params(dict(BASE, **extra))
    assert result["error_code"] == "update_key_required"
    assert result["steps"] == {}
    ddl.assert_not_called()
    odi.assert_not_called()


def test_skey_pins_merge_during_validation():
    result = mg.MappingGenerator().validate_params(dict(
        BASE, tgt_table="SKEY_TEST", needs_skey=True, skey_natural_key="ID",
        ikm_name="IKM Oracle Insert", update_key_columns=["NATURAL_KEY"],
    ))
    assert result["ikm_name"] == mg.SKEY_LOOKUP_IKM


@pytest.mark.parametrize("priority", ["", "missing,oracle merge", "oracle,oracle insert"])
def test_unresolved_or_ambiguous_priority_fails_before_ddl(priority, monkeypatch):
    monkeypatch.setattr(mg, "ODI_IKM_PRIORITY", priority)
    generator = mg.MappingGenerator()
    ddl = Mock(side_effect=AssertionError("DDL reached"))
    monkeypatch.setattr(generator, "_create_target_table", ddl)
    result = generator.generate_from_params(dict(BASE, update_key_columns=["ID"]))
    assert result["overall_status"] == "FAILED"
    assert "ikm_name" in result["error"]
    ddl.assert_not_called()


def test_insert_first_is_pinned_not_a_repository_fallback(monkeypatch):
    monkeypatch.setattr(mg, "ODI_IKM_PRIORITY", "oracle insert,oracle merge")
    result = mg.MappingGenerator().validate_params(BASE)
    assert result["ikm_name"] == "IKM Oracle Insert"
    run = Mock(return_value="SUCCESS")
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    oo.OdiOrchestrator().generate_mapping("MAP_X", "A", "B", "C", "D", ikm_name=result["ikm_name"])
    sent = run.call_args.args[1]
    assert sent["odi.ikm_name"] == "IKM Oracle Insert"
    assert "odi.ikm_priority" not in sent


@pytest.mark.parametrize("name", ["IKM Oracle Merge Custom", "IKM Evil", "IKM Oracle Insert\nX", 7])
def test_unapproved_name_never_reaches_groovy(name, monkeypatch):
    run = Mock(side_effect=AssertionError("Groovy reached"))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    with pytest.raises(ValueError):
        oo.OdiOrchestrator().generate_mapping("MAP_X", "A", "B", "C", "D", ikm_name=name)
    run.assert_not_called()


def test_exact_case_insensitive_approval_and_removed_default():
    assert resolve_approved_ikm(" ikm oracle merge ", "IU", APPROVED, "") == "IKM Oracle Merge"
    with pytest.raises(ValueError):
        resolve_approved_ikm(None, "IU", "IKM Oracle Insert", "oracle merge,oracle insert")
    with pytest.raises(ValueError):
        resolve_approved_ikm(None, "CONTROL_APPEND", "IKM Oracle Merge", "oracle merge")


@pytest.mark.parametrize("kind", ["PRIMARY_KEY", "ALTERNATE_KEY"])
def test_key_type_transported_to_groovy(kind, monkeypatch):
    run = Mock(return_value="SUCCESS")
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    oo.OdiOrchestrator().generate_mapping(
        "MAP_X", "A", "B", "C", "D", required_update_key_columns=["id"], required_update_key_type=kind,
    )
    sent = run.call_args.args[1]
    assert sent["odi.required_update_key_type"] == kind
    assert sent["odi.required_update_key_columns"] == "ID"
    assert sent["odi.ikm_name"] == "IKM Oracle Merge"


@pytest.mark.parametrize("fatal", [
    "SEVERE: unrelated transaction failed",
    "SEVERE: Registration denied for javax.management.j2ee.statistics.Stats",
    "ERROR: javax/management/j2ee/statistics/Stats failed to register",
    "FATAL: javax.management.j2ee.statistics.Stats",
    '{"status":"error","detail":"javax.management.j2ee.statistics.Stats"}',
    "Caused by: java.lang.IllegalStateException: unrelated failure",
    "Suppressed: java.io.IOException: unrelated failure",
])
def test_jmx_noise_never_consumes_adjacent_failure(fatal):
    assert oo._groovy_output_failed(KNOWN + "\n" + fatal + '\n{"status":"passed"}')


def test_known_jmx_noise_is_removed_including_header_and_ellipsis():
    assert oo._strip_known_jmx_noise(KNOWN + '\n{"status":"passed"}') == '{"status":"passed"}'
    assert not oo._groovy_output_failed(KNOWN)


@pytest.mark.parametrize("trace", [
    "java.lang.NoClassDefFoundError: org/example/Other\n\tat javax.management.j2ee.Stats.run(Stats.java:1)",
    "java.lang.IllegalStateException: javax.management.j2ee.statistics.Stats",
    "java.lang.NoClassDefFoundError: javax/management/j2ee/statistics/Stats unrelated text",
])
def test_only_precise_missing_class_trace_is_ignored(trace):
    assert oo._groovy_output_failed("SEVERE: Error initializing the JMX Framework SPI\n" + trace)


class Cursor:
    def __init__(self, constraints, columns):
        self.constraints, self.columns = constraints, columns
        self.sql = ""
        self.statements = []

    def execute(self, sql, binds=None):
        assert sql.lstrip().startswith("SELECT"), "Existing targets must never receive DDL"
        self.sql = sql
        self.statements.append((sql, binds))

    def fetchone(self):
        return (1,)

    def fetchall(self):
        if "ALL_CONSTRAINTS" in self.sql:
            return self.constraints
        return [(name,) for name in self.columns]

    def close(self):
        pass


@pytest.mark.parametrize("rows,columns,key,code", [
    ([], ["ID", "NAME"], ["ID"], "update_key_missing_on_existing_target"),
    ([("PK_T", "ID", "P", "ENABLED", "VALIDATED")], ["ID", "NAME"], ["ID"], "update_key_kind_mismatch"),
    ([("UK_T", "ID", "U", "DISABLED", "VALIDATED")], ["ID", "NAME"], ["ID"], "update_key_constraint_disabled"),
    ([("UK_T", "ID", "U", "ENABLED", "NOT VALIDATED")], ["ID", "NAME"], ["ID"], "update_key_constraint_disabled"),
    ([("UK_T", "ID", "U", "ENABLED", "VALIDATED")], ["ID"], ["ID"], "target_columns_missing_on_existing_target"),
    ([], ["ID"], [], "target_columns_missing_on_existing_target"),
    ([("UK_T", "ID", "U", "ENABLED", "VALIDATED")], ["ID", "NAME"], ["ID"], None),
    ([], ["ID", "NAME"], [], None),
])
def test_existing_target_contract_executes_selects_only(rows, columns, key, code, monkeypatch):
    cursor = Cursor(rows, columns)
    connection = Mock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(ts, "_connection", lambda: connection)
    result = ts.execute_target_table_plan(mg.MappingGenerator(), BASE, dict(PLAN, unique_key=key), "DWH")
    assert result["status"] == ("BLOCKED" if code else "ALREADY_IN_SYNC")
    assert result.get("code") == code
    assert result["ddl"] == "" and result["added_columns"] == []
    connection.commit.assert_not_called()


def test_blocked_schema_stops_pipeline_and_preserves_detail(monkeypatch):
    generator = mg.MappingGenerator()
    monkeypatch.setattr(generator, "_preflight_odi_repository", lambda _: {"status": "passed"})
    monkeypatch.setattr(generator, "_build_target_column_plan", lambda _: PLAN)
    monkeypatch.setattr(ts, "_target_exists", lambda _: True)
    monkeypatch.setattr(ts, "_existing_key_constraints", lambda _: [
        dict(name="UK_T", columns=["ID"], type="U", enabled=True, validated=True),
    ])
    monkeypatch.setattr(ts, "_target_columns", lambda _: {"ID"})
    monkeypatch.setattr(generator, "_resolve_odi_repository_bindings", lambda _: {"status": "passed"})
    odi = Mock(side_effect=AssertionError("ODI writes reached"))
    monkeypatch.setattr(generator, "_register_odi_datastores", odi)
    result = generator.generate_from_params(dict(BASE, update_key_columns=["ID"]))
    assert result["error_code"] == "target_columns_missing_on_existing_target"
    assert result["steps"]["create_target_table"]["missing_columns"] == ["NAME"]
    odi.assert_not_called()


def test_concurrent_create_conflict_does_not_grant_or_report_success(monkeypatch):
    import oracledb

    connection = Mock()
    connection.cursor.return_value.execute.side_effect = oracledb.DatabaseError("ORA-00955")
    monkeypatch.setattr(ts, "_connection", lambda: connection)
    monkeypatch.setattr(ts, "_target_exists", lambda _: False)
    grants = Mock(side_effect=AssertionError("grants reached"))
    monkeypatch.setattr(ts, "_apply_grants", grants)
    with pytest.raises(mg.MappingGenerationError):
        ts.execute_target_table_plan(mg.MappingGenerator(), BASE, PLAN, "DWH")
    grants.assert_not_called()
    connection.commit.assert_not_called()
