from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import odi_orchestrator
import scd2_builder
from odi_orchestrator import OdiOrchestrator
from tests.scd2_support import REQUEST, SOURCE_COLUMNS, error_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GROOVY_ROOT = PROJECT_ROOT / "odi_automation"
SCD2_SCRIPTS = ("scd2_probe.groovy", "scd2_datastore_register.groovy", "scd2_mapping_create.groovy")


def read(name: str) -> str:
    return (GROOVY_ROOT / name).read_text(encoding="utf-8")


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _prepare_runtime(monkeypatch, completed):
    monkeypatch.setattr(odi_orchestrator, "_JAVA_BIN", r"C:\Java\bin\java.exe")
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(odi_orchestrator, "_ensure_local_props_file", lambda: r"D:\safe\x.properties")
    calls = []
    monkeypatch.setattr(
        odi_orchestrator.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or completed
    )
    return calls


# ------------------------------------------------------------------ registry


def test_scripts_are_registered_with_expected_categories():
    registry = odi_orchestrator.GROOVY_ACTION_REGISTRY
    assert registry["scd2_probe.groovy"].category == "read_only"
    assert registry["scd2_probe.groovy"].approval_required is False
    for name in ("scd2_datastore_register.groovy", "scd2_mapping_create.groovy"):
        assert registry[name].category == "repository_write"
        assert registry[name].approval_required is True


def test_strict_error_contracts_cover_every_scd2_script():
    for name in SCD2_SCRIPTS:
        contract = odi_orchestrator._STRICT_STRUCTURED_ERROR_CONTRACTS[name]
        assert contract["tool"].startswith("odi.scd2_")
        assert "error_type" in contract["keys"] and "side_effects" in contract["keys"]


def test_reviewed_structured_error_is_preserved_and_extras_are_rejected(monkeypatch):
    safe = error_payload("odi.scd2_mapping_create", "generic_absent_only_scd2_mapping_creation", "IkmUnavailable")
    _prepare_runtime(monkeypatch, _Completed(stdout=safe))
    with odi_orchestrator.groovy_approval_context(True):
        assert OdiOrchestrator().run_groovy("scd2_mapping_create.groovy", {}) == safe

    unsafe = json.loads(safe)
    unsafe["error_detail"] = "internal path"
    _prepare_runtime(monkeypatch, _Completed(stdout=json.dumps(unsafe)))
    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy("scd2_mapping_create.groovy", {})
    assert result == "Hata: ODI Groovy action failed. error_type=GroovyFatalOutput"

    wrong_tool = json.loads(safe)
    wrong_tool["tool"] = "odi.scd2_probe"
    _prepare_runtime(monkeypatch, _Completed(stdout=json.dumps(wrong_tool)))
    with odi_orchestrator.groovy_approval_context(True):
        assert OdiOrchestrator().run_groovy("scd2_mapping_create.groovy", {}).endswith("GroovyFatalOutput")


def test_write_scripts_are_blocked_without_approval(monkeypatch):
    calls = _prepare_runtime(monkeypatch, _Completed(stdout="{}"))
    for name in ("scd2_datastore_register.groovy", "scd2_mapping_create.groovy"):
        assert OdiOrchestrator().run_groovy(name, {}).endswith("error_type=ApprovalRequired")
    assert calls == []


def test_probe_runs_without_approval_and_transports_plan_parameters(monkeypatch):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    calls = _prepare_runtime(monkeypatch, _Completed(stdout="{}"))
    params = scd2_builder.groovy_params(plan)
    OdiOrchestrator().run_groovy("scd2_probe.groovy", params)
    command = calls[0][0]
    assert "-Dodi.scd2.target_model=TGT_DM" in command
    assert any(part.startswith("-Dodi.scd2.column_types=") for part in command)
    assert command[-2].endswith("scd2_probe.groovy")


# ----------------------------------------------------------- script contracts


@pytest.mark.parametrize("name", SCD2_SCRIPTS)
def test_scripts_take_parameters_from_the_plan_not_fixed_identities(name):
    text = read(name)
    assert 'System.getProperty("odi.scd2.' in text or 'requireProp("odi.scd2.' in text
    for fixed in ("DIM_PRODUCT", "ODS_SH_PRODUCTS", "PRODUCT_SK", "PROD_ID", "MAP_ODS_PRODUCTS"):
        assert fixed not in text, fixed
    assert "TGT_DWH" not in text  # model codes arrive as parameters


@pytest.mark.parametrize("name", SCD2_SCRIPTS)
def test_scripts_emit_structured_value_free_json(name):
    text = read(name)
    assert "JsonOutput.toJson" in text
    assert "raw_rows_included: false" in text and "pk_values_included: false" in text
    assert "secret_printed: false" in text
    assert "error_type: reasonCode(e)" in text
    assert text.count("getMessage()") == 1  # only inside reasonCode, never echoed raw
    # No row access and no destructive repository calls.
    for forbidden in ("executeQuery", "SELECT ", ".remove(", "delete", "em.remove", "DROP ", "TRUNCATE"):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("name", ("scd2_datastore_register.groovy", "scd2_mapping_create.groovy"))
def test_write_scripts_use_one_odi_transaction_with_rollback(name):
    text = read(name)
    assert text.count("txnMgr.getTransaction(") == 1
    assert "txnMgr.commit(txnStatus)" in text
    assert "txnMgr.rollback(txnStatus)" in text


def test_probe_is_read_only_and_rolls_back():
    text = read("scd2_probe.groovy")
    assert "txnMgr.commit" not in text and "em.persist" not in text
    assert "txnMgr.rollback(txnStatus)" in text
    assert "side_effects: false" in text


def test_register_script_registers_pk_alternate_key_and_all_scd_roles():
    text = read("scd2_datastore_register.groovy")
    assert "OdiKey.KeyType.PRIMARY_KEY" in text and "OdiKey.KeyType.ALTERNATE_KEY" in text
    for role in (
        "SURROGATE_KEY", "NATURAL_KEY", "ADD_ROW_ON_CHANGE", "OVERWRITE_ON_CHANGE",
        "START_TIMESTAMP", "END_TIMESTAMP", "CURRENT_RECORD_FLAG",
    ):
        assert role in text, role
    assert "TargetDatastoreAlreadyExists" in text  # absent-only
    assert "TargetModelUnavailable" in text


def test_mapping_script_is_scd_with_the_reviewed_kms_and_absent_only():
    text = read("scd2_mapping_create.groovy")
    assert "IKM Oracle Slowly Changing Dimension" in text
    assert "LKM SQL to Oracle (Built-In)" in text
    assert "DatastoreComponent.INTEGRATION_TYPE_SCD" in text
    assert "SLOWLY_CHANGING_DIMENSION" in text
    assert "MappingAlreadyExists" in text and "ScenarioAlreadyExists" in text
    assert "mapping.validate()" in text
    assert "TO_DATE('24000101','YYYYMMDD')" in text
    assert 'odiRef.getSession(\\"SESS_NO\\")' in text
    assert "NEXTVAL" in text


# ------------------------------------------------------- compile against SDK


def _sdk_classpath():
    home = Path(os.getenv("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    groovy = home / "oracle_common" / "modules" / "thirdparty" / os.getenv("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    sdk = home / "odi" / "sdk" / "lib"
    jdbc = home / "oracle_common" / "modules" / "oracle.jdbc" / os.getenv("OJDBC_JAR_NAME", "ojdbc8.jar")
    java = shutil.which("java")
    if not (java and groovy.exists() and sdk.exists()):
        return None, None
    return java, os.pathsep.join([str(sdk / "*"), str(groovy), str(jdbc)])


@pytest.mark.parametrize("name", SCD2_SCRIPTS)
def test_scripts_compile_against_the_odi_sdk_when_available(name):
    """Parse/compile only (groovyc); nothing is executed and no repository is opened."""
    java, classpath = _sdk_classpath()
    if java is None:
        pytest.skip("ODI SDK or Groovy jar not available in this environment")
    with tempfile.TemporaryDirectory() as out:
        completed = subprocess.run(
            [java, "-cp", classpath, "org.codehaus.groovy.tools.FileSystemCompiler", "-d", out, str(GROOVY_ROOT / name)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    assert completed.returncode == 0, completed.stderr[-500:]


@pytest.mark.parametrize("name", SCD2_SCRIPTS)
def test_script_delimiters_are_balanced(name):
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', read(name))
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"/\^.*?\$/|~/.*?/", "", text)
    for open_char, close_char in ("{}", "()", "[]"):
        assert text.count(open_char) == text.count(close_char), name


@pytest.mark.parametrize("name", SCD2_SCRIPTS)
def test_error_type_is_an_allowlisted_code_or_exception_class_name_never_the_message(name):
    text = read(name)
    assert "final Set<String> REASON_CODES" in text
    body = text.split("def reasonCode")[1].split("def requireProp")[0]
    assert "REASON_CODES.contains(message)" in body
    assert "e.getClass().getSimpleName()" in body
    assert "return message\n" in body and "contains(message)" in body  # message only after allowlist check
    codes = re.search(r"REASON_CODES = \[(.*?)\] as Set", text, re.S).group(1)
    assert all(re.fullmatch(r'"[A-Za-z][A-Za-z0-9]{0,63}"', c.strip()) for c in codes.split(","))


def test_probe_returns_contract_metadata_for_existing_objects():
    text = read("scd2_probe.groovy")
    for needle in ("scenario_versions", "scenario_name_collision", "target_columns", "target_primary_key",
                   "target_alternate_key", "integration_type", "target_ikm", "lkm_names",
                   "source_tables", "source_components", "target_component", "getDataFormat()",
                   "getSourceMappingId()", "findBySourceMapping"):
        assert needle in text, needle
    assert "getModel()?.getCode()?.equalsIgnoreCase(targetModel)" in text
    assert "txnMgr.commit" not in text


def test_create_forces_builtin_lkm_on_every_final_access_point_and_fails_on_db_link():
    text = read("scd2_mapping_create.groovy")
    assert "design.getAllAPNodes()" in text
    assert "node.setLKM(lkm)" in text and "getLKMName()" in text
    assert "LkmDbLinkRemains" in text and '"LkmDbLinkRemains"' in text.split("REASON_CODES = [")[1].split("] as Set")[0]
    # assignment happens after the last physical-design reset
    assert text.index("design.resetSpec()") < text.index("design.getAllAPNodes()")
    assert text.index("design.getAllAPNodes()") < text.index("txnMgr.commit")


def test_probe_reports_access_point_count():
    text = read("scd2_probe.groovy")
    assert "access_point_count: accessPointCount" in text
    line = text.split("lkmNames = apNodes")[1].split(chr(10))[0]
    assert "it.getLKMName()" in line and "getXKMName" not in line and "unique()" not in line
