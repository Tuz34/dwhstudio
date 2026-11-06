"""Generic SCD2 stable audit creation expressions (ODI-87 K3).

The CASE text is produced by the real ``stableAuditText`` closure in the Groovy scripts and then
executed on SQLite. A small adapter maps Oracle's null-safe ``DECODE(a, b, 0, 1) = 0`` to SQLite's
``a IS b`` and substitutes the ODI session token and ``CURRENT_TIMESTAMP`` with fixed markers.
Native Oracle semantics stay separate: an ambiguous match (two current versions) raises ORA-01427
in Oracle's scalar subquery, while SQLite returns one row. That case is documented, not emulated.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

import odi81_app_acceptance as driver
import odi81_scd2_fixtures as f81
import scd2_builder
import scd2_odi
import scd2_service
from scd2_manager import _TRACKED_ATTRIBUTES
from tests import dim_product_mapping_fake_sdk as fake
from tests.scd2_support import (
    REQUEST,
    STABLE_AUDIT_PROFILE,
    FakeOrchestrator,
    make_services,
    mapping_payload,
    probe_payload,
)
from tests.test_generic_repository_writer_sdk import SCENARIOS, _params, _run

ROOT = Path(__file__).resolve().parents[1] / "odi_automation"
BUILDER = ROOT / "scd2_mapping_create.groovy"
PROBE = ROOT / "scd2_probe.groovy"
BEGIN, END = "// ODI87-STABLE-AUDIT-BEGIN", "// ODI87-STABLE-AUDIT-END"
AUDITS = ("INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID")
DEFAULT_TS, DEFAULT_BATCH = "DEFAULT_TS", "DEFAULT_BATCH"


def _groovy_command():
    java = fake.java_binary()
    if java is None or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Java/Groovy unavailable; nothing is downloaded")
    return [java, "-cp", str(fake.GROOVY_JAR), "groovy.ui.GroovyMain"]


def _block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text[text.index(BEGIN):text.index(END) + len(END)]


def _evaluate(tmp_path, script: str, payload: dict) -> dict:
    path = tmp_path / f"harness_{abs(hash(script)) % 10**8}.groovy"
    path.write_text(script, encoding="utf-8")
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP", "PATH") if key in os.environ}
    done = subprocess.run([*_groovy_command(), str(path)], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=120, check=False, env=environment)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


TEXTS_HARNESS = '''
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
def input = new JsonSlurper().parse(System.in)
''' + "__BLOCK__" + '''
def results = [:]
input.cases.each { name, spec ->
    try {
        results[name] = ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"].collectEntries { column ->
            [(column): stableAuditText(column, spec.table, spec.alias, spec.keys, spec.tracked, spec.overwrite)]
        }
    } catch (IllegalStateException rejected) {
        results[name] = [rejected: rejected.getMessage()]
    }
}
println JsonOutput.toJson(results)
'''


def stable_texts(tmp_path, cases: dict) -> dict:
    """Run the builder's real closure (byte-identical with the probe's) for each case."""
    script = TEXTS_HARNESS.replace("__BLOCK__", _block(BUILDER))
    return _evaluate(tmp_path, script, {"cases": cases})


# --------------------------------------------------------------------------- SQLite adapter

def to_sqlite(text: str) -> str:
    adapted = re.sub(r"DECODE\(([^,()]+), ([^,()]+), 0, 1\) = 0", r"(\1 IS \2)", text)
    assert "DECODE" not in adapted
    adapted = adapted.replace('<%=odiRef.getSession("SESS_NO")%>', f"'{DEFAULT_BATCH}'")
    return adapted.replace("CURRENT_TIMESTAMP", f"'{DEFAULT_TS}'")


class Store:
    """Target schema attached as the owner and a source table named like the ODI alias."""

    def __init__(self, owner, table, alias, columns):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(f"ATTACH DATABASE ':memory:' AS {owner}")
        self.columns = columns
        target_columns = ["SK", *columns, "CURRENT_FLAG", *AUDITS]
        self.db.execute(f"CREATE TABLE {owner}.{table} ({', '.join(target_columns)})")
        self.db.execute(f"CREATE TABLE {alias} ({', '.join(columns)})")
        self.owner, self.table, self.alias = owner, table, alias

    def target(self, *rows):
        names = ["SK", *self.columns, "CURRENT_FLAG", *AUDITS]
        for row in rows:
            self.db.execute(f"INSERT INTO {self.owner}.{self.table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                            [row.get(name) for name in names])

    def source(self, row):
        self.db.execute(f"DELETE FROM {self.alias}")
        self.db.execute(f"INSERT INTO {self.alias} ({', '.join(self.columns)}) VALUES ({', '.join('?' * len(self.columns))})",
                        [row.get(name) for name in self.columns])

    def audits(self, texts):
        return {column: self.db.execute(f"SELECT {to_sqlite(texts[column])} FROM {self.alias} {self.alias}").fetchone()[0]
                for column in AUDITS}


OLD = {"INSERT_DATE": "OLD_INS", "UPDATE_DATE": "OLD_UPD", "SOURCE_SYSTEM": "OLD_SYS", "ETL_BATCH_ID": "OLD_BATCH"}
DEFAULTS = {"INSERT_DATE": DEFAULT_TS, "UPDATE_DATE": DEFAULT_TS, "SOURCE_SYSTEM": "ODI_SCD2", "ETL_BATCH_ID": DEFAULT_BATCH}
KEEP_VERSION = {"INSERT_DATE": "OLD_INS", "UPDATE_DATE": DEFAULT_TS, "SOURCE_SYSTEM": "OLD_SYS", "ETL_BATCH_ID": DEFAULT_BATCH}


def _f4_like(spec_flow):
    spec = f81.FLOWS[spec_flow]
    return {"table": f"{spec['target_owner']}.{spec['target_table']}", "alias": spec["source_table"],
            "keys": ["ENTITY_ID"], "tracked": ["ATTR_TEXT", "VALUE_AMT"], "overwrite": []}


COMPOSITE = {"table": "DM.DIM_COMPOSITE", "alias": "SRC_COMPOSITE", "keys": ["K2", "K1"],
             "tracked": ["T2", "T1"], "overwrite": ["OW1"]}


@pytest.fixture(scope="module")
def texts(tmp_path_factory):
    cases = {"F4": _f4_like("F4"), "F5": _f4_like("F5"), "COMPOSITE": COMPOSITE,
             "COMPOSITE_REORDERED": {**COMPOSITE, "keys": ["K1", "K2"], "tracked": ["T1", "T2"]},
             "ALIAS_COLLIDES": {**COMPOSITE, "alias": "SCD2_CUR"},
             "BAD_IDENTIFIER": {**COMPOSITE, "tracked": ["T1;DROP"]},
             "NO_KEYS": {**COMPOSITE, "keys": []}}
    return stable_texts(tmp_path_factory.mktemp("stable_texts"), cases)


def test_expression_texts_are_case_guards_with_null_safe_tracked_comparisons(texts):
    for flow in ("F4", "F5"):
        for column in AUDITS:
            text = texts[flow][column]
            assert text.startswith("CASE WHEN EXISTS (SELECT 1 FROM ")
            assert "SCD2_CUR.CURRENT_FLAG = '1'" in text
            assert "DECODE(SCD2_CUR.ATTR_TEXT, " in text and "DECODE(SCD2_CUR.VALUE_AMT, " in text
            for forbidden in ("ROWNUM", "MAX(", "MIN(", "COALESCE", "NVL(", "FETCH FIRST"):
                assert forbidden not in text
    assert texts["F5"]["INSERT_DATE"].count(" FROM DM.DIM_ODI81N_F5 SCD2_CUR ") == 2
    # Sorted identifiers make the text independent of plan versus datastore-role order.
    assert texts["COMPOSITE"] == texts["COMPOSITE_REORDERED"]
    assert "DECODE(SCD2_CUR.OW1, SRC_COMPOSITE.OW1, 0, 1) = 0" in texts["COMPOSITE"]["UPDATE_DATE"]
    assert "OW1" not in texts["COMPOSITE"]["INSERT_DATE"] and "OW1" not in texts["COMPOSITE"]["SOURCE_SYSTEM"]
    for case in ("ALIAS_COLLIDES", "BAD_IDENTIFIER", "NO_KEYS"):
        assert texts[case] == {"rejected": "SourceAliasInvalid"}


@pytest.mark.parametrize("flow", ["F4", "F5"])
def test_f4_f5_unchanged_rows_keep_all_audits_and_changes_take_creation_defaults(texts, flow):
    spec = _f4_like(flow)
    owner, table = spec["table"].split(".")
    store = Store(owner, table, spec["alias"], ["ENTITY_ID", "ATTR_TEXT", "VALUE_AMT"])
    store.target(
        {"SK": 1, "ENTITY_ID": 101, "ATTR_TEXT": "A", "VALUE_AMT": 10, "CURRENT_FLAG": "1", **OLD},
        {"SK": 2, "ENTITY_ID": 102, "ATTR_TEXT": None, "VALUE_AMT": 20, "CURRENT_FLAG": "1", **OLD},
        {"SK": 3, "ENTITY_ID": 103, "ATTR_TEXT": "C", "VALUE_AMT": 30, "CURRENT_FLAG": "0", **OLD},
        {"SK": 4, "ENTITY_ID": 104, "ATTR_TEXT": "D", "VALUE_AMT": 40, "CURRENT_FLAG": "1",
         "INSERT_DATE": None, "UPDATE_DATE": None, "SOURCE_SYSTEM": None, "ETL_BATCH_ID": None},
    )
    cases = [
        ({"ENTITY_ID": 101, "ATTR_TEXT": "A", "VALUE_AMT": 10}, OLD),                  # unchanged
        ({"ENTITY_ID": 102, "ATTR_TEXT": None, "VALUE_AMT": 20}, OLD),                 # NULL equals NULL
        ({"ENTITY_ID": 102, "ATTR_TEXT": "NOW_SET", "VALUE_AMT": 20}, DEFAULTS),       # NULL to value
        ({"ENTITY_ID": 101, "ATTR_TEXT": None, "VALUE_AMT": 10}, DEFAULTS),            # value to NULL
        ({"ENTITY_ID": 101, "ATTR_TEXT": "A", "VALUE_AMT": 11}, DEFAULTS),             # tracked change
        ({"ENTITY_ID": 999, "ATTR_TEXT": "A", "VALUE_AMT": 10}, DEFAULTS),             # new key
        ({"ENTITY_ID": 103, "ATTR_TEXT": "C", "VALUE_AMT": 30}, DEFAULTS),             # only a history row
        ({"ENTITY_ID": 104, "ATTR_TEXT": "D", "VALUE_AMT": 40},                        # NULL audits kept NULL
         dict.fromkeys(AUDITS)),
    ]
    for row, expected in cases:
        store.source(row)
        assert store.audits(texts[flow]) == expected, row


def test_composite_key_and_user_overwrite_attributes(texts):
    store = Store("DM", "DIM_COMPOSITE", "SRC_COMPOSITE", ["K1", "K2", "T1", "T2", "OW1"])
    store.target({"SK": 1, "K1": 1, "K2": "A", "T1": "x", "T2": None, "OW1": "o", "CURRENT_FLAG": "1", **OLD})
    cases = [
        ({"K1": 1, "K2": "A", "T1": "x", "T2": None, "OW1": "o"}, OLD),                 # same version, no delta
        ({"K1": 1, "K2": "A", "T1": "x", "T2": None, "OW1": "changed"}, KEEP_VERSION),  # type-1 overwrite delta
        ({"K1": 1, "K2": "A", "T1": "x", "T2": None, "OW1": None}, KEEP_VERSION),       # overwrite to NULL
        ({"K1": 1, "K2": "A", "T1": "y", "T2": None, "OW1": "o"}, DEFAULTS),            # tracked change
        ({"K1": 1, "K2": "B", "T1": "x", "T2": None, "OW1": "o"}, DEFAULTS),            # one key part differs
        ({"K1": 2, "K2": "A", "T1": "x", "T2": None, "OW1": "o"}, DEFAULTS),            # other key part differs
    ]
    for row, expected in cases:
        store.source(row)
        assert store.audits(texts["COMPOSITE"]) == expected, row


# --------------------------------------------------------------------------- full builder script

def _builder_run(tmp_path, cases):
    classes = dict(fake._CLASSES)
    finder = "oracle.odi.domain.model.finder.IOdiDataStoreFinder"
    classes[finder] = classes[finder].replace(
        "INSERT_DATE: null, UPDATE_DATE: null, SOURCE_SYSTEM: null, ETL_BATCH_ID: null",
        ", ".join(name + ": OdiColumn.ScdType.OVERWRITE_ON_CHANGE" for name in AUDITS))
    java = fake.java_binary()
    if java is None or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Groovy unavailable; nothing downloaded")
    return _run(java, tmp_path, "scd2_mapping_create.groovy", classes, _params(), cases)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    cases = {
        "clean": SCENARIOS["clean"],
        "tamper_insert_date": {"tamper_expression": "INSERT_DATE"},
        "tamper_etl_batch": {"tamper_expression": "ETL_BATCH_ID"},
        "tamper_tracked": {"tamper_expression": _TRACKED_ATTRIBUTES[0]},
        "audit_hint_target": {"tamper_hint": "UPDATE_DATE"},
    }
    return _builder_run(tmp_path_factory.mktemp("stable_builder"), cases)


def test_builder_commits_exactly_the_stable_profile_and_freezes_other_expressions(built, tmp_path):
    run = built["clean"]
    report = json.loads(run["output"])
    assert report["status"] == "passed" and run["committed"] is True
    assert report["result"]["stable_audit_expression_count"] == 4
    expected = stable_texts(tmp_path, {"plan": {
        "table": "DWH.DIM_PRODUCT", "alias": "ODS_SH_PRODUCTS", "keys": ["PROD_ID"],
        "tracked": list(_TRACKED_ATTRIBUTES), "overwrite": []}})["plan"]
    expressions = run["expressions"]
    assert {column: expressions[column] for column in AUDITS} == expected
    assert expressions["PROD_ID"] == "ODS_SH_PRODUCTS.PROD_ID"
    assert all(expressions[name] == f"ODS_SH_PRODUCTS.{name}" for name in _TRACKED_ATTRIBUTES)
    assert expressions["PRODUCT_SK"] == "DWH.SEQ_TEST.NEXTVAL"
    assert expressions["EFFECTIVE_FROM"] == "CURRENT_DATE"
    assert expressions["EFFECTIVE_TO"] == "TO_DATE('24000101','YYYYMMDD')"
    assert expressions["CURRENT_FLAG"] == "1"
    # The report is value-free: no expression text reaches the output.
    assert "CASE WHEN" not in run["output"] and "SCD2_CUR" not in run["output"]


@pytest.mark.parametrize("case", ["tamper_insert_date", "tamper_etl_batch", "tamper_tracked", "audit_hint_target"])
def test_final_validation_tampering_never_commits(built, case):
    run = built[case]
    report = json.loads(run["output"])
    assert run["committed"] is False and "commit" not in run["trace"]
    assert report["error_type"] == "MappingExpressionDrift"
    assert report["repository_outcome"] == "rolled_back" and report["side_effects"] is False


# --------------------------------------------------------------------------- probe profile fragment

PROFILE_START = "            // Stable audit profile, re-derived"
PROFILE_END = "            mappingInfo = ["


def _probe_profile_script() -> str:
    text = PROBE.read_text(encoding="utf-8")
    assert text.count(PROFILE_START) == 1 and text.count(PROFILE_END) == 1
    fragment = text[text.index(PROFILE_START):text.index(PROFILE_END)]
    return '''
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
final java.util.regex.Pattern ID = ~/^[A-Z0-9_#]{1,30}$/
final List<String> AUDIT_OVERWRITE_COLUMNS = ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"]
''' + _block(PROBE) + '''
def input = new JsonSlurper().parse(System.in)
def results = [:]
input.cases.each { name, spec ->
    String targetOwner = spec.owner
    String targetTable = spec.table
    def fakeColumn = { Map c -> new Expando(getName: { c.name }, getScdType: { c.role == null ? null : [name: { c.role }] }) }
    def tgtStore = new Expando(getColumns: { spec.columns.collect { fakeColumn(it) } })
    def fakeAttribute = { Map a -> new Expando(getName: { a.name },
        getExpression: { a.text == null ? null : new Expando(getText: { a.text }) },
        getExecuteOnHint: { a.hint == null ? null : [name: { a.hint }] }) }
    def targets = [new Expando(getAttributes: { spec.attributes.collect { fakeAttribute(it) } })]
    def sources = spec.sources.collect { alias -> new Expando(getName: { alias }) }
''' + fragment + '''
    results[name] = auditProfile
}
println JsonOutput.toJson(results)
'''


def _probe_case(texts_for_plan, **change):
    columns = [{"name": "ENTITY_ID", "role": "NATURAL_KEY"}, {"name": "ATTR_TEXT", "role": "ADD_ROW_ON_CHANGE"},
               {"name": "VALUE_AMT", "role": "ADD_ROW_ON_CHANGE"}, {"name": "CURRENT_FLAG", "role": "CURRENT_RECORD_FLAG"},
               *({"name": name, "role": "OVERWRITE_ON_CHANGE"} for name in AUDITS)]
    attributes = [{"name": name, "text": texts_for_plan[name], "hint": None} for name in AUDITS]
    case = {"owner": "DWH", "table": "DIM_ODI81N_F4", "sources": ["ODS_ODI81N_F4"], "columns": columns, "attributes": attributes}
    for key, value in change.items():
        case[key] = value(case) if callable(value) else value
    return case


def test_probe_reports_only_booleans_and_counts_for_the_stable_profile(texts, tmp_path):
    plan = texts["F4"]
    volatile = {**plan, "UPDATE_DATE": "CURRENT_TIMESTAMP"}
    cases = {
        "stable": _probe_case(plan),
        "one_volatile": _probe_case(volatile),
        "missing_audit": _probe_case(plan, attributes=lambda c: c["attributes"][:3]),
        "target_hint": _probe_case(plan, attributes=lambda c: [{**c["attributes"][0], "hint": "TARGET"}, *c["attributes"][1:]]),
        "alias_collides": _probe_case(plan, sources=["SCD2_CUR"]),
        "two_sources": _probe_case(plan, sources=["ODS_ODI81N_F4", "OTHER"]),
        "audit_role_missing": _probe_case(plan, columns=lambda c: [x for x in c["columns"] if x["name"] != "ETL_BATCH_ID"]),
        "no_natural_key": _probe_case(plan, columns=lambda c: [x for x in c["columns"] if x["role"] != "NATURAL_KEY"]),
        "other_owner": _probe_case(plan, owner="DM"),
        "user_overwrite_unaccounted": _probe_case(plan, columns=lambda c: [*c["columns"], {"name": "OW1", "role": "OVERWRITE_ON_CHANGE"}]),
    }
    results = _evaluate(tmp_path, _probe_profile_script(), {"cases": cases})
    keys = {"stable", "role_contract_valid", "source_alias_valid", "audit_attribute_count", "stable_expression_count",
            "audit_target_hint_count"}
    assert all(set(result) == keys for result in results.values())
    assert results["stable"] == {**STABLE_AUDIT_PROFILE}
    assert results["one_volatile"]["stable"] is False and results["one_volatile"]["stable_expression_count"] == 3
    assert results["missing_audit"]["stable"] is False and results["missing_audit"]["audit_attribute_count"] == 3
    assert results["target_hint"]["stable"] is False and results["target_hint"]["audit_target_hint_count"] == 1
    assert results["alias_collides"]["source_alias_valid"] is False and results["alias_collides"]["stable"] is False
    assert results["two_sources"]["stable"] is False and results["two_sources"]["audit_attribute_count"] == 0
    assert results["audit_role_missing"]["role_contract_valid"] is False and results["audit_role_missing"]["stable"] is False
    assert results["no_natural_key"]["role_contract_valid"] is False
    # Profiles are bound to the physical target and to the user overwrite roles of the datastore.
    assert results["other_owner"]["stable"] is False and results["other_owner"]["stable_expression_count"] == 0
    assert results["user_overwrite_unaccounted"]["stable_expression_count"] == 2
    assert "CASE" not in json.dumps(results)


# --------------------------------------------------------------------------- Python metadata contract

def _mapping_probe(profile):
    payload = json.loads(probe_payload(target_datastore_exists=True, mapping_exists=True, scenario_exists=True))
    if profile is None:
        del payload["result"]["mapping"]["audit_profile"]
    else:
        payload["result"]["mapping"]["audit_profile"] = profile
    return json.dumps(payload)


@pytest.mark.parametrize("profile", [
    None,
    {**STABLE_AUDIT_PROFILE, "extra": 1},
    {**STABLE_AUDIT_PROFILE, "stable": "true"},
    {**STABLE_AUDIT_PROFILE, "stable_expression_count": -1},
    {**STABLE_AUDIT_PROFILE, "audit_target_hint_count": True},
])
def test_malformed_or_missing_profile_fails_the_probe_closed(profile):
    from tests.scd2_support import _default_plan
    orchestrator = FakeOrchestrator(_default_plan(), responses={"scd2_probe.groovy": _mapping_probe(profile)})
    params = scd2_builder.probe_params("DWH", "ODS_CUSTOMERS", "DM", "DIM_CUSTOMER", "MAP_DIM_CUSTOMER")
    step = scd2_odi.probe(orchestrator, params)
    assert step["status"] == "failed" and step["side_effects"] is False


@pytest.mark.parametrize("change", [
    {"stable": False}, {"role_contract_valid": False}, {"source_alias_valid": False},
    {"audit_attribute_count": 3}, {"stable_expression_count": 3}, {"audit_target_hint_count": 1},
    {"stable_expression_count": 5},
])
def test_any_non_stable_profile_blocks_reuse_even_when_flagged_stable(change):
    mapping = {"audit_profile": {**STABLE_AUDIT_PROFILE, **change}}
    assert scd2_odi.audit_profile_stable(mapping) is False
    assert scd2_odi.audit_profile_stable({"audit_profile": dict(STABLE_AUDIT_PROFILE)}) is True
    assert scd2_odi.audit_profile_stable(None) is False


def _flow_run_spec(flow):
    return scd2_service.validate_run_request(driver.run_body(flow, "GLOBAL"))


@pytest.mark.parametrize("flow", ["F4", "F5"])
@pytest.mark.parametrize("stable", [True, False])
def test_f4_f5_reuse_path_requires_the_stable_profile(flow, stable):
    spec = _flow_run_spec(flow)
    profile = dict(STABLE_AUDIT_PROFILE) if stable else {**STABLE_AUDIT_PROFILE, "stable": False, "stable_expression_count": 0}
    plan_like = {"source_model": spec["source_model"], "source_table": spec["source_table"],
                 "target_model": spec["target_model"], "target_table": spec["target_table"]}

    def response(params):
        assert params["odi.scd2.target_owner"] == f81.FLOWS[flow]["target_owner"]
        payload = json.loads(probe_payload(mapping_exists=True, scenario_exists=True))
        payload["result"]["mapping"].update(
            target_table=spec["target_table"], source_tables=[spec["source_table"]],
            target_component={"model": spec["target_model"], "table": spec["target_table"]},
            source_components=[{"model": spec["source_model"], "table": spec["source_table"]}],
            audit_profile=profile)
        return json.dumps(payload)

    from tests.scd2_support import _default_plan
    step = scd2_odi.probe(FakeOrchestrator(_default_plan(), responses={"scd2_probe.groovy": response}), spec["probe"])
    assert step["status"] == "passed"
    mismatches = scd2_odi.mapping_mismatches(step["mapping"], plan_like)
    assert ("mapping_audit_profile" in mismatches) is (not stable)
    assert "CASE" not in json.dumps(step)


@pytest.mark.parametrize("stable", [True, False])
def test_generate_route_reuses_only_a_stable_existing_mapping(stable):
    from tests.scd2_support import _default_plan
    from tests.test_scd2_service import ddl_handler

    plan = _default_plan()
    profile = dict(STABLE_AUDIT_PROFILE) if stable else {**STABLE_AUDIT_PROFILE, "stable": False, "stable_expression_count": 1}
    payload = json.loads(probe_payload(plan, target_datastore_exists=True, mapping_exists=True, scenario_exists=True))
    payload["result"]["mapping"]["audit_profile"] = profile
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": json.dumps(payload)},
                                    scenario_result={"status": "already_ready"})
    result, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1, sequence_exists=1)), approved=True)
    if stable:
        assert status == 200 and result["status"] == "generated"
    else:
        assert status == 409 and result["status"] == "blocked_existing_object_mismatch"
        assert "mapping_audit_profile" in result["mismatches"]
        assert "scd2_mapping_create.groovy" not in orchestrator.order


@pytest.mark.parametrize("count", [0, 3, 5, None])
def test_created_mapping_must_report_four_stable_audit_expressions(count):
    from tests.scd2_support import _default_plan

    plan = _default_plan()
    payload = json.loads(mapping_payload(plan))
    if count is None:
        del payload["result"]["stable_audit_expression_count"]
    else:
        payload["result"]["stable_audit_expression_count"] = count
    orchestrator = FakeOrchestrator(plan, responses={"scd2_mapping_create.groovy": json.dumps(payload)})
    step = scd2_odi.create_mapping(orchestrator, plan, True)
    assert step["status"] == "failed" and step["side_effects"] is True
