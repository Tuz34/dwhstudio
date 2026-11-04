from pathlib import Path

import json
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _create_mapping_lkm_block() -> str:
    text = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(encoding="utf-8")
    start = text.index("// LKM:")
    end = text.index("try {\n            // ODI", start)
    return text[start:end]


def _update_mapping_lkm_block() -> str:
    text = (PROJECT_ROOT / "odi_automation" / "update_mapping.groovy").read_text(encoding="utf-8")
    start = text.index("// Change the LKM")
    end = text.index("break", start)
    return text[start:end]


def test_create_mapping_lkm_assignment_is_not_silent():
    block = _create_mapping_lkm_block()

    assert "catch (Exception ig) {}" not in block
    assert "LKM VERIFY" in block
    assert "getLKM()?.getName()" in block
    assert "getLKMName()" in block
    assert "PHYSICAL NODES REFRESHED BEFORE LKM" in block
    assert "LKM NOT REQUIRED: no access point" in block
    assert "MapPhysicalNode lkmTarget = apNode" in block


def test_create_mapping_ikm_assignment_is_verified_and_required():
    text = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(
        encoding="utf-8"
    )

    assert "IKM VERIFY" in text
    assert "getIKM()?.getName()" in text
    assert "setIKMByName" in text
    assert "design.setStagingLocationName(targetLocationName)" in text
    assert "design.resetSpec()" in text
    assert "IKM selection failed" in text


def test_create_mapping_requires_requested_update_key_columns():
    text = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(
        encoding="utf-8"
    )

    assert 'System.getProperty("odi.required_update_key_columns")' in text
    assert "Required update key is missing or mismatched" in text
    assert "setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_UPDATE)" in text
    assert "INTEGRATION TYPE VERIFY" in text


def test_create_mapping_never_replaces_existing_mapping_without_exact_flag():
    text = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(
        encoding="utf-8"
    )

    assert 'System.getProperty("odi.replace_existing_mapping", "false")' in text
    assert "Mapping already exists and replacement was not approved" in text


def test_create_mapping_blocks_db_link_lkm_for_same_database_flows():
    block = _create_mapping_lkm_block()

    assert "DB Link" in block or "DBLINK" in block
    assert "throw new Exception" in block


def test_create_mapping_fatal_errors_exit_nonzero():
    text = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(encoding="utf-8")
    catch_start = text.index("} catch (Exception e) {")
    catch_block = text[catch_start:]

    assert '"status":"error"' in catch_block
    assert '"action":"create_mapping"' in catch_block
    assert '"error_type":"' in catch_block
    assert "System.exit(1)" in catch_block


def test_update_mapping_lkm_repair_verifies_assignment():
    block = _update_mapping_lkm_block()

    assert "LKM VERIFY" in block
    assert "getLKM()?.getName()" in block
    assert "getLKMName()" in block
    assert "DB Link" in block or "DBLINK" in block
    assert "throw new Exception" in block


def test_create_mapping_truncate_target_option_is_parameterized():
    content = (PROJECT_ROOT / "odi_automation" / "create_mapping.groovy").read_text(encoding="utf-8")

    assert 'System.getProperty("odi.truncate_target_table")' in content
    assert 'System.getProperty("odi.truncate_target_table", "true")' not in content
    assert "TRUNCATE_TARGET_TABLE" in content
    assert "getIKMOptionValue" in content
    assert "setValue(Boolean.valueOf" in content


def test_update_mapping_truncate_target_option_can_be_set_without_reselecting_km():
    content = (PROJECT_ROOT / "odi_automation" / "update_mapping.groovy").read_text(encoding="utf-8")
    block = _update_mapping_lkm_block()

    assert 'System.getProperty("odi.truncate_target_table")' in content
    assert "TRUNCATE_TARGET_TABLE" in block
    assert "getIKMOptionValue" in content
    assert "setValue(Boolean.valueOf" in content
    assert 'setIkmOptionIfRequested(tgtNode, "TRUNCATE_TARGET_TABLE", truncateTargetTableParam)' in block
    assert "ikmNameParam && !lkmNameParam && !truncateTargetTableParam" in content


def test_update_mapping_supports_scoped_rename_without_physical_ddl():
    content = (PROJECT_ROOT / "odi_automation" / "update_mapping.groovy").read_text(encoding="utf-8")

    assert 'System.getProperty("odi.rename_source_table_name")' in content
    assert 'System.getProperty("odi.rename_target_table_name")' in content
    assert 'System.getProperty("odi.rename_column_side")' in content
    assert 'System.getProperty("odi.rename_column_old_name")' in content
    assert 'System.getProperty("odi.rename_column_new_name")' in content
    assert 'renameComponentIfRequested(srcComp, newSourceName, "SOURCE")' in content
    assert 'renameComponentIfRequested(tgtComp, newTargetName, "TARGET")' in content
    assert "COMPONENT RENAMED" in content
    assert "COLUMN RENAMED" in content
    assert "ALTER TABLE" not in content.upper()


def test_sync_datastore_column_supports_type_and_rename_metadata_sync():
    content = (PROJECT_ROOT / "odi_automation" / "sync_datastore_column.groovy").read_text(encoding="utf-8")

    assert 'System.getProperty("odi.operation")' in content
    assert 'System.getProperty("odi.column_type")' in content
    assert 'System.getProperty("odi.column_length")' in content
    assert 'System.getProperty("odi.new_name")' in content
    assert "setDataTypeCode" in content
    assert "setLength" in content
    assert "setScale" in content
    assert "setName(newName" in content
    assert "setResourceName(newName" in content
    assert "txnMgr.commit" in content
    assert "ALTER TABLE" not in content.upper()


def test_update_mapping_uses_wrapper_properties_path():
    content = (PROJECT_ROOT / "odi_automation" / "update_mapping.groovy").read_text(encoding="utf-8")

    assert 'args?.length ? args[0] : "odi_connection.properties"' in content
    assert "c:/oracle_ai_app" not in content.lower()


def test_diagnose_ikm_uses_wrapper_properties_path_and_exits_nonzero():
    content = (PROJECT_ROOT / "odi_automation" / "diagnose_ikm.groovy").read_text(encoding="utf-8")

    assert 'args?.length ? args[0] : "odi_connection.properties"' in content
    assert "c:/oracle_ai_app" not in content.lower()
    assert "System.exit(1)" in content


def test_run_scenario_uses_runtime_agent_and_fails_hard():
    content = (PROJECT_ROOT / "odi_automation" / "run_scenario.groovy").read_text(encoding="utf-8")

    assert "RuntimeAgent" in content
    assert "String.class, char[].class" in content
    assert "Integer.class" in content
    assert "Boolean.TYPE" in content
    assert "SCENARIO_EXISTS" not in content
    assert "System.exit(1)" in content


def test_seed_physical_topology_context_mapping_compares_data_server_not_schema_only():
    content = (PROJECT_ROOT / "odi_automation" / "seed_physical_topology.groovy").read_text(encoding="utf-8")

    assert "getDataServer()?.getName()" in content
    assert "mappedDataServerName?.equalsIgnoreCase(dataServerName)" in content
    assert "mappedSchemaName?.equalsIgnoreCase(schemaName)" in content


def test_create_load_plan_metadata_is_metadata_only_and_allowlisted():
    content = (PROJECT_ROOT / "odi_automation" / "create_load_plan_metadata.groovy").read_text(encoding="utf-8")

    assert "OdiLoadPlan" in content
    assert "addStepRunScenario" in content
    assert "ALLOWED_SCENARIOS" in content
    assert "MAP_HR_EMPLOYEES_TO_ODS_SCN" in content
    assert "MAP_SH_SALES_TO_ODS_SCN" in content
    assert "startLoadPlan" not in content
    assert "startScenario" not in content
    assert "RuntimeAgent" not in content
    assert "em.remove" not in content
    assert "DROP " not in content.upper()
    assert "TRUNCATE " not in content.upper()


def test_load_plan_probe_is_read_only():
    content = (PROJECT_ROOT / "odi_automation" / "load_plan_probe.groovy").read_text(encoding="utf-8")

    assert "IOdiLoadPlanFinder" in content
    assert "getChildrenSteps" in content
    assert "startLoadPlan" not in content
    assert "startScenario" not in content
    assert "RuntimeAgent" not in content
    assert "persist(" not in content
    assert "commit(" not in content
    assert "em.remove" not in content


def test_run_load_plan_remote_is_allowlisted_and_redacted():
    content = (PROJECT_ROOT / "odi_automation" / "run_load_plan_remote.groovy").read_text(encoding="utf-8")

    assert "RemoteRuntimeAgentInvoker" in content
    assert "LoadPlanStartupParams" in content
    assert "invokeStartLoadPlan" in content
    assert "ALLOWED_LOAD_PLANS" in content
    assert "LP_HR_SH_ODS_LOAD" in content
    assert "invokeStartScenario" not in content
    assert "startScenario" not in content
    assert "new RuntimeAgent" not in content
    assert "OdiLoadPlan(" not in content
    assert "persist(" not in content
    assert "commit(" not in content
    assert "em.remove" not in content
    assert "DROP " not in content.upper()
    assert "TRUNCATE " not in content.upper()
    assert "replaceAll(/jdbc:" in content


def test_run_load_plan_remote_handles_compact_status_codes():
    content = (PROJECT_ROOT / "odi_automation" / "run_load_plan_remote.groovy").read_text(encoding="utf-8")

    assert "RUNNING_STATUS_CODES" in content
    assert '"W"' in content
    assert '"R"' in content
    assert "DONE_STATUS_CODES" in content
    assert '"D"' in content


DIM_PRODUCT_RUN_SCRIPT = "dim_product_scd2_scenario_run.groovy"
DIM_PRODUCT_RUN_SCOPE = "odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof"
DIM_PRODUCT_SCENARIO = {
    "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
    "version": "001",
    "context": "GLOBAL",
}


def _dim_product_run_payload(*, status="passed", side_effects=True):
    payload = {
        "tool": "odi.dim_product_scd2_scenario_run",
        "status": status,
        "mode": "fixed_scd2_scenario_run",
        "side_effects": side_effects,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "scenario": DIM_PRODUCT_SCENARIO.copy(),
    }
    if status == "passed":
        payload["session_status"] = "DONE"
    else:
        payload["error_type"] = "ScenarioRunFailed"
    return payload


def test_dim_product_scenario_run_script_is_fixed_and_registered():
    import odi_orchestrator

    script = (PROJECT_ROOT / "odi_automation" / DIM_PRODUCT_RUN_SCRIPT).read_text(
        encoding="utf-8"
    )
    lowered = script.lower()
    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[DIM_PRODUCT_RUN_SCRIPT].category == "scenario_run"
    assert DIM_PRODUCT_RUN_SCRIPT in odi_orchestrator._STRICT_STRUCTURED_ERROR_CONTRACTS
    assert 'final String SCENARIO_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN"' in script
    assert 'final String SCENARIO_VERSION = "001"' in script
    assert 'final String CONTEXT_CODE = "GLOBAL"' in script
    assert "RemoteRuntimeAgentInvoker" in script
    assert "invokeStartScenario" in script
    assert "JsonOutput.toJson" in script
    for forbidden in (
        "getinternalid",
        "getsessionid()",
        "system.getproperty(\"odi.scenario",
        "println e",
        "printstacktrace",
        "drop ",
        "truncate ",
        "delete ",
        ".remove(",
    ):
        assert forbidden not in lowered


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved": False},
        {"approved": "yes"},
        {"approved": 1},
        {"approval_scope": "odi68_products_ods_scenario_run_and_rerun_proof"},
        {"scenario_name": "OTHER_SCN"},
        {"version": "002"},
        {"context": "OTHER"},
    ],
)
def test_dim_product_scenario_run_wrapper_blocks_invalid_requests_before_groovy(
    monkeypatch, overrides
):
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []
    monkeypatch.setattr(orchestrator, "run_groovy", lambda *args: calls.append(args))
    request = {
        "approved": True,
        "approval_scope": DIM_PRODUCT_RUN_SCOPE,
        "scenario_name": DIM_PRODUCT_SCENARIO["name"],
        "version": "001",
        "context": "GLOBAL",
    }
    request.update(overrides)

    result = orchestrator.run_dim_product_scd2_scenario_safe(**request)

    assert result["status"].startswith("blocked")
    assert result["side_effects"] is False
    assert result["session_id_included"] is False
    assert calls == []


def test_dim_product_scenario_run_wrapper_returns_only_fixed_success(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []

    def fake_run(script, params):
        calls.append((script, params, odi_orchestrator._GROOVY_APPROVED.get()))
        return json.dumps(_dim_product_run_payload())

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)
    result = orchestrator.run_dim_product_scd2_scenario_safe(
        approved=True,
        approval_scope=DIM_PRODUCT_RUN_SCOPE,
        scenario_name=DIM_PRODUCT_SCENARIO["name"],
        version="001",
        context="GLOBAL",
    )

    assert calls == [(DIM_PRODUCT_RUN_SCRIPT, {}, True)]
    assert result == {
        "status": "passed",
        "scenario": DIM_PRODUCT_SCENARIO,
        "session_status": "DONE",
        "side_effects": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "raw_output_included": False,
        "session_id_included": False,
    }


@pytest.mark.parametrize(
    "raw_output",
    [
        "SUCCESS: unstructured",
        json.dumps({**_dim_product_run_payload(), "session_id": "hidden"}),
        json.dumps({**_dim_product_run_payload(), "session_status": "RUNNING"}),
        json.dumps({**_dim_product_run_payload(), "raw_rows_included": True}),
        json.dumps({**_dim_product_run_payload(), "scenario": {"name": "OTHER", "version": "001", "context": "GLOBAL"}}),
        json.dumps({**_dim_product_run_payload(status="error"), "error_type": ["unsafe"]}),
    ],
)
def test_dim_product_scenario_run_wrapper_rejects_unsafe_output(monkeypatch, raw_output):
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    monkeypatch.setattr(orchestrator, "run_groovy", lambda *_: raw_output)
    result = orchestrator.run_dim_product_scd2_scenario_safe(
        approved=True,
        approval_scope=DIM_PRODUCT_RUN_SCOPE,
        scenario_name=DIM_PRODUCT_SCENARIO["name"],
        version="001",
        context="GLOBAL",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "InvalidScenarioRunOutput"
    assert "hidden" not in json.dumps(result)
    assert "OTHER" not in json.dumps(result)


def test_dim_product_scenario_run_script_sdk_parse_only():
    """Compile the Groovy source when a local ODI SDK exists; never execute it."""
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    sdk_jar = Path(odi_orchestrator._ODI_SDK_LIB) / "odi-core.jar"
    groovy_jar = Path(odi_orchestrator._GROOVY_JAR)
    java = Path(odi_orchestrator._JAVA_BIN)
    if not (sdk_jar.is_file() and groovy_jar.is_file() and java.is_file()):
        pytest.skip("Local ODI SDK/Groovy jars are unavailable")

    script = PROJECT_ROOT / "odi_automation" / DIM_PRODUCT_RUN_SCRIPT
    command = [
        str(java),
        "-cp",
        OdiOrchestrator()._build_classpath(),
        "groovy.ui.GroovyMain",
        "-e",
        "new GroovyShell().parse(new File(args[0])); println('PARSED')",
        str(script),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr[-1000:]
    assert "PARSED" in result.stdout
