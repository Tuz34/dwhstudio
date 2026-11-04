import json
from pathlib import Path

import odi_orchestrator
from odi_orchestrator import OdiOrchestrator


def test_read_only_probe_uses_whitelisted_script_and_safe_params(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": False,
            "repository_connection_attempted": True,
            "checks": [],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.read_only_probe(
        approved=True,
        project="TUTORIOL",
        folder="First Folder",
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        src_model="ORACLE_HR",
        src_table="EMPLOYEES",
        tgt_model="DWH",
        tgt_table="ODS_HR_EMPLOYEES",
    )

    assert result["status"] == "passed"
    assert result["side_effects"] is False
    assert calls == [
        (
            "read_only_probe.groovy",
            {
                "odi.project": "TUTORIOL",
                "odi.folder": "First Folder",
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
                "odi.src_model": "ORACLE_HR",
                "odi.src_table": "EMPLOYEES",
                "odi.tgt_model": "DWH",
                "odi.tgt_table": "ODS_HR_EMPLOYEES",
            },
        )
    ]


def test_read_only_probe_script_is_allowlisted():
    assert "read_only_probe.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "inspect_mapping_km_options.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "seed_project_folder.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "seed_model_datastore.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "repair_datastore_metadata.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "seed_physical_topology.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "create_mapping_smoke.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "repair_mapping_components.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "generate_scenario_metadata.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "scenario_probe.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "create_load_plan_metadata.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "load_plan_probe.groovy" in odi_orchestrator._ALLOWED_SCRIPTS
    assert "run_load_plan_remote.groovy" in odi_orchestrator._ALLOWED_SCRIPTS


def test_resolve_java_bin_checks_nested_jdk(monkeypatch):
    existing = r"C:\Program Files\Java\jdk1.8.0_211\bin\java.exe"

    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda path: path == existing)
    monkeypatch.setattr(odi_orchestrator.shutil, "which", lambda _name: None)

    assert odi_orchestrator._resolve_java_bin(r"C:\Program Files\Java") == existing


def test_read_only_probe_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.read_only_probe(mapping_name="MAP_X")

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_REPOSITORY_READ_ONLY"


def test_read_only_probe_invalid_json_returns_structured_error(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "not json")

    result = orch.read_only_probe(approved=True, mapping_name="MAP_X")

    checks = {item["name"]: item for item in result["checks"]}
    assert result["status"] == "error"
    assert result["side_effects"] is False
    assert result["repository_connection_attempted"] is True
    assert checks["read_only_probe_json"]["status"] == "fail"


def test_inspect_mapping_km_options_uses_read_only_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": False,
            "repository_connection_attempted": True,
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "km_options": {"TRUNCATE_TARGET_TABLE": True},
            "physical": {
                "target_node": "ODS_HR_EMPLOYEES",
                "target_ikm": "IKM Oracle Insert.GLOBAL",
                "lkm": "LKM SQL to Oracle",
            },
            "checks": [{"name": "mapping_found", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.inspect_mapping_km_options(
        approved=True,
        project="ORA_DWH",
        folder="HR",
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
    )

    assert result["status"] == "passed"
    assert result["side_effects"] is False
    assert result["km_options"]["TRUNCATE_TARGET_TABLE"] is True
    assert calls == [
        (
            "inspect_mapping_km_options.groovy",
            {
                "odi.project": "ORA_DWH",
                "odi.folder": "HR",
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
            },
        )
    ]


def test_inspect_mapping_km_options_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.inspect_mapping_km_options(mapping_name="MAP_X")

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_REPOSITORY_READ_ONLY"


def test_inspect_mapping_km_options_invalid_json_returns_structured_error(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "not json")

    result = orch.inspect_mapping_km_options(approved=True, mapping_name="MAP_X")

    checks = {item["name"]: item for item in result["checks"]}
    assert result["status"] == "error"
    assert result["side_effects"] is False
    assert result["repository_connection_attempted"] is True
    assert checks["inspect_mapping_km_options_json"]["status"] == "fail"


def test_run_groovy_treats_fatal_stdout_as_error(monkeypatch):
    class FakeCompletedProcess:
        returncode = 0
        stdout = "FATAL ERROR during create_mapping: LKM VERIFY FAILED"
        stderr = ""

    monkeypatch.setattr(odi_orchestrator, "_JAVA_BIN", r"C:\Java\bin\java.exe")
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        odi_orchestrator,
        "_ensure_local_props_file",
        lambda: r"D:\safe\odi_connection.local.properties",
    )
    monkeypatch.setattr(
        odi_orchestrator.subprocess,
        "run",
        lambda *_args, **_kwargs: FakeCompletedProcess(),
    )

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy("create_mapping.groovy", {})

    assert result.startswith("Hata:")
    assert result.endswith("error_type=GroovyFatalOutput")
    assert "LKM VERIFY FAILED" not in result


def test_update_mapping_passes_truncate_target_table_option(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return "OK"

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.update_mapping(
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        operation="update_km",
        truncate_target_table=True,
    )

    assert result == "OK"
    assert calls == [
        (
            "update_mapping.groovy",
            {
                "odi.project": odi_orchestrator.ODI_PROJECT,
                "odi.folder": odi_orchestrator.ODI_FOLDER,
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
                "odi.operation": "update_km",
                "odi.truncate_target_table": "true",
            },
        )
    ]


def test_update_mapping_passes_scoped_rename_options(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return "OK"

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.update_mapping(
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        operation="rename",
        new_mapping_name="MAP_HR_EMPLOYEES_TO_ODS_V2",
        rename_source_table_name="EMP_SRC",
        rename_target_table_name="EMP_TGT",
        rename_column_side="target",
        rename_column_old_name="EMPLOYEE_ID",
        rename_column_new_name="EMP_ID",
    )

    assert result == "OK"
    assert calls == [
        (
            "update_mapping.groovy",
            {
                "odi.project": odi_orchestrator.ODI_PROJECT,
                "odi.folder": odi_orchestrator.ODI_FOLDER,
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
                "odi.operation": "rename",
                "odi.new_mapping_name": "MAP_HR_EMPLOYEES_TO_ODS_V2",
                "odi.rename_source_table_name": "EMP_SRC",
                "odi.rename_target_table_name": "EMP_TGT",
                "odi.rename_column_side": "target",
                "odi.rename_column_old_name": "EMPLOYEE_ID",
                "odi.rename_column_new_name": "EMP_ID",
            },
        )
    ]


def test_sync_datastore_column_passes_modify_type_options(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return "OK"

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.sync_datastore_column(
        model_code="TGT_DWH",
        table_name="ODS_HR_EMPLOYEES",
        operation="modify_type",
        column_name="FIRST_NAME",
        column_type="VARCHAR2",
        column_length=30,
    )

    assert result == "OK"
    assert calls == [
        (
            "sync_datastore_column.groovy",
            {
                "odi.model_code": "TGT_DWH",
                "odi.table_name": "ODS_HR_EMPLOYEES",
                "odi.operation": "modify_type",
                "odi.column_name": "FIRST_NAME",
                "odi.column_type": "VARCHAR2",
                "odi.column_length": "30",
            },
        )
    ]


def test_read_only_probe_groovy_has_no_repository_mutations():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "read_only_probe.groovy"
    content = script_path.read_text(encoding="utf-8")

    forbidden_tokens = [
        ".persist(",
        ".remove(",
        ".commit(",
        "createScenario",
        "new Mapping(",
        "new DatastoreComponent(",
    ]
    for token in forbidden_tokens:
        assert token not in content

    assert "error_detail" in content
    assert "replaceAll(/jdbc:" in content


def test_inspect_mapping_km_options_groovy_is_read_only_and_reads_truncate_option():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "inspect_mapping_km_options.groovy"
    content = script_path.read_text(encoding="utf-8")

    forbidden_tokens = [
        ".persist(",
        ".remove(",
        ".commit(",
        "txnMgr.commit",
        "generateScenario",
        "startScenario",
        "startLoadPlan",
        "new Mapping(",
        "new DatastoreComponent(",
        "setIKM",
        "setLKM",
        "setValue(",
    ]
    for token in forbidden_tokens:
        assert token not in content

    assert "getIKMOptionValue(\"TRUNCATE_TARGET_TABLE\")" in content
    assert "getIKM()?.getName()" in content
    assert "getLKM()?.getName()" in content
    assert "getIntegrationType()" in content
    assert "getUpdateKey()" in content
    assert "ODS_CREATED_AT" in content
    assert "ODS_UPDATED_AT" in content
    assert "side_effects: false" in content


def test_list_projects_uses_wrapper_properties_path():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "list_projects.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert 'args?.length ? args[0] : "odi_connection.properties"' in content
    assert "c:/oracle_ai_app" not in content.lower()


def test_seed_project_folder_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.seed_project_folder(project="TUTORIOL", folder="First Folder")

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_seed_project_folder_uses_seed_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "project_created", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.seed_project_folder(
        approved=True,
        project="TUTORIOL",
        folder="First Folder",
        project_name="TUTORIOL",
    )

    assert result["status"] == "passed"
    assert result["side_effects"] is True
    assert calls == [
        (
            "seed_project_folder.groovy",
            {
                "odi.project": "TUTORIOL",
                "odi.folder": "First Folder",
                "odi.project_name": "TUTORIOL",
            },
        )
    ]


def test_seed_project_folder_groovy_is_idempotent_and_scoped():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "seed_project_folder.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "new OdiProject" in content
    assert "new OdiFolder" in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()


def test_seed_model_datastore_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.seed_model_datastore(
        model_code="ORACLE_HR",
        table_name="EMPLOYEES",
        columns=[{"name": "EMPLOYEE_ID", "type": "NUMBER"}],
    )

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_seed_model_datastore_uses_seed_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "model_created", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.seed_model_datastore(
        approved=True,
        model_code="ORACLE_HR",
        model_name="ORACLE_HR",
        logical_schema="LGL_ORACLE_HR",
        table_name="EMPLOYEES",
        resource_name="EMPLOYEES",
        columns=[
            {"name": "EMPLOYEE_ID", "type": "NUMBER", "length": 6, "scale": 0},
            {"name": "FIRST_NAME", "type": "VARCHAR2", "length": 20},
        ],
        update_key_name="UK_EMPLOYEES_EMPLOYEE_ID",
        update_key_columns=["EMPLOYEE_ID"],
    )

    assert result["status"] == "passed"
    assert result["side_effects"] is True
    assert calls == [
        (
            "seed_model_datastore.groovy",
            {
                "odi.model_code": "ORACLE_HR",
                "odi.model_name": "ORACLE_HR",
                "odi.logical_schema": "LGL_ORACLE_HR",
                "odi.table_name": "EMPLOYEES",
                "odi.resource_name": "EMPLOYEES",
                "odi.columns": "EMPLOYEE_ID:NUMBER:6,FIRST_NAME:VARCHAR2:20",
                "odi.update_key_name": "UK_EMPLOYEES_EMPLOYEE_ID",
                "odi.update_key_columns": "EMPLOYEE_ID",
                "odi.technology": "Oracle",
                "odi.tech_code": "ORACLE",
            },
        )
    ]


def test_seed_model_datastore_omits_empty_columns_param(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({"status": "passed", "checks": []})

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.seed_model_datastore(
        approved=True,
        model_code="DWH",
        table_name="ODS_HR_EMPLOYEES",
        columns=[],
    )

    assert result["status"] == "passed"
    assert "odi.columns" not in calls[0][1]


def test_seed_model_datastore_groovy_is_idempotent_and_scoped():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "seed_model_datastore.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "new OdiLogicalSchema" in content
    assert "new OdiModel" in content
    assert "new OdiDataStore" in content
    assert "new OdiColumn" in content
    assert "new OdiKey" in content
    assert "OdiKey.KeyType.ALTERNATE_KEY" in content
    assert "update_key_verified" in content
    assert "setDataFormat" in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()


def test_repair_datastore_metadata_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.repair_datastore_metadata(model_code="ORACLE_HR", table_name="EMPLOYEES")

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_repair_datastore_metadata_uses_repair_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "column_data_formats_repaired", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.repair_datastore_metadata(
        approved=True,
        model_code="DWH",
        table_name="ODS_HR_EMPLOYEES",
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            "repair_datastore_metadata.groovy",
            {
                "odi.model_code": "DWH",
                "odi.table_name": "ODS_HR_EMPLOYEES",
            },
        )
    ]


def test_repair_datastore_metadata_groovy_is_scoped_and_non_destructive():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "repair_datastore_metadata.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "setDataFormat" in content
    assert "setTableFormatType" in content
    assert "new OdiDataStore" not in content
    assert "new OdiColumn" not in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()


def test_seed_physical_topology_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.seed_physical_topology(
        data_server="ORACLE_HR_DS",
        schema_name="HR",
        logical_schema="LGL_ORACLE_HR",
        env_prefix="SOURCE_ORACLE",
    )

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_seed_physical_topology_uses_safe_non_secret_params(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "physical_schema_created", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.seed_physical_topology(
        approved=True,
        data_server="DWH_DS",
        schema_name="DWH",
        logical_schema="LGL_DWH",
        env_prefix="TARGET_ORACLE",
        context="GLOBAL",
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            "seed_physical_topology.groovy",
            {
                "odi.data_server": "DWH_DS",
                "odi.schema_name": "DWH",
                "odi.work_schema_name": "DWH",
                "odi.logical_schema": "LGL_DWH",
                "odi.env_prefix": "TARGET_ORACLE",
                "odi.context": "GLOBAL",
                "odi.technology": "Oracle",
            },
        )
    ]
    assert not any("PASSWORD" in key.upper() or "DSN" in key.upper() for key in calls[0][1])


def test_seed_physical_topology_groovy_is_scoped_and_redacted():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "seed_physical_topology.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "new OdiDataServer" in content
    assert "new OdiPhysicalSchema" in content
    assert "new OdiContextualSchemaMapping" in content
    assert "ObfuscatedString.obfuscate" in content
    assert 'JdbcSettings(normalizeJdbcUrl(jdbcDsn), "oracle.jdbc.OracleDriver")' in content
    assert "data_server_connection_updated" in content
    assert "jdbc:[^" in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()


def test_create_mapping_smoke_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.create_mapping_smoke(
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        src_model="ORACLE_HR",
        src_table="EMPLOYEES",
        tgt_model="DWH",
        tgt_table="ODS_HR_EMPLOYEES",
    )

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_create_mapping_smoke_uses_smoke_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "commit_completed", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.create_mapping_smoke(
        approved=True,
        project="TUTORIOL",
        folder="First Folder",
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        src_model="ORACLE_HR",
        src_table="EMPLOYEES",
        tgt_model="DWH",
        tgt_table="ODS_HR_EMPLOYEES",
    )

    assert result["status"] == "passed"
    assert result["side_effects"] is True
    assert calls == [
        (
            "create_mapping_smoke.groovy",
            {
                "odi.project": "TUTORIOL",
                "odi.folder": "First Folder",
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
                "odi.src_model": "ORACLE_HR",
                "odi.src_table": "EMPLOYEES",
                "odi.tgt_model": "DWH",
                "odi.tgt_table": "ODS_HR_EMPLOYEES",
            },
        )
    ]


def test_create_mapping_smoke_groovy_is_absent_only_and_non_destructive():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "create_mapping_smoke.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "new Mapping" in content
    assert "new DatastoreComponent" in content
    assert "Mapping already exists" in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()
    assert "generateScenario" not in content
    assert "startScenario" not in content


def test_repair_mapping_components_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.repair_mapping_components(
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        src_model="ORACLE_HR",
        src_table="EMPLOYEES",
        tgt_model="DWH",
        tgt_table="ODS_HR_EMPLOYEES",
    )

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_repair_mapping_components_uses_repair_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "components_connected", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.repair_mapping_components(
        approved=True,
        project="TUTORIOL",
        folder="First Folder",
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        src_model="ORACLE_HR",
        src_table="EMPLOYEES",
        tgt_model="DWH",
        tgt_table="ODS_HR_EMPLOYEES",
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            "repair_mapping_components.groovy",
            {
                "odi.project": "TUTORIOL",
                "odi.folder": "First Folder",
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
                "odi.src_model": "ORACLE_HR",
                "odi.src_table": "EMPLOYEES",
                "odi.tgt_model": "DWH",
                "odi.tgt_table": "ODS_HR_EMPLOYEES",
            },
        )
    ]


def test_repair_mapping_components_groovy_is_empty_mapping_only_and_non_destructive():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "repair_mapping_components.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "new DatastoreComponent" in content
    assert "new DatastoreComponent(owner, ds, false)" in content
    assert "Mapping already has components" in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()
    assert "generateScenario" not in content
    assert "startScenario" not in content


def test_generate_scenario_metadata_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.generate_scenario_metadata(
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        scenario_name="SCN_MAP_HR_EMPLOYEES_TO_ODS",
    )

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_CREATE_MAPPING"


def test_generate_scenario_metadata_uses_safe_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": True,
            "repository_connection_attempted": True,
            "checks": [{"name": "scenario_generated", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.generate_scenario_metadata(
        approved=True,
        project="TUTORIOL",
        folder="First Folder",
        mapping_name="MAP_HR_EMPLOYEES_TO_ODS",
        scenario_name="SCN_MAP_HR_EMPLOYEES_TO_ODS",
        version="001",
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            "generate_scenario_metadata.groovy",
            {
                "odi.project": "TUTORIOL",
                "odi.folder": "First Folder",
                "odi.mapping": "MAP_HR_EMPLOYEES_TO_ODS",
                "odi.scenario.name": "SCN_MAP_HR_EMPLOYEES_TO_ODS",
                "odi.scenario.version": "001",
            },
        )
    ]


def test_generate_scenario_legacy_wrapper_uses_absent_only_safe_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({"status": "passed", "checks": []})

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.generate_scenario("MAP_X_SCN", "MAP_X", "001")

    assert result == "SUCCESS: Scenario MAP_X_SCN version 001 is ready."
    assert calls[0][0] == "generate_scenario_metadata.groovy"
    assert calls[0][1]["odi.folder"] == odi_orchestrator.ODI_FOLDER


def test_destructive_legacy_scenario_generator_is_not_available():
    legacy = Path(__file__).resolve().parents[1] / "odi_automation" / "generate_scenario.groovy"

    assert "generate_scenario.groovy" not in odi_orchestrator._ALLOWED_SCRIPTS
    assert not legacy.exists()


def test_generate_scenario_metadata_invalid_json_returns_structured_error(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "not json")

    result = orch.generate_scenario_metadata(
        approved=True,
        mapping_name="MAP_X",
        scenario_name="SCN_MAP_X",
    )

    checks = {item["name"]: item for item in result["checks"]}
    assert result["status"] == "error"
    assert result["side_effects"] is True
    assert checks["generate_scenario_metadata_json"]["status"] == "fail"


def test_generate_scenario_metadata_groovy_is_non_destructive_and_no_run():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "generate_scenario_metadata.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "ScenarioUtils.generateNewScen" in content
    assert "OdiScenarioGeneratorImpl" not in content
    assert "generateScenario(" not in content
    assert "TransactionalDwgObjectTemplate" in content
    assert "new DefaultTransactionDefinition(2)" in content
    assert "getTransaction(outerTxnDef)" in content
    assert "getTransactionalEntityManager().flush()" in content
    assert "MappingCompatibilityHelper.setCompatibleMappingEditable(mapping, true)" in content
    assert "MappingCompatibilityHelper.setReferencedMappingEditable(node, true)" in content
    assert "txnMgr.commit" in content
    assert "rollback(txnStatus)" in content
    assert "findLatestByName" in content
    assert "getVersion()?.equalsIgnoreCase(version)" in content
    assert "findByUniqueName" not in content
    assert ".remove(" not in content
    assert "delete" not in content.lower()
    assert "drop" not in content.lower()
    assert "truncate" not in content.lower()
    assert "startScenario" not in content
    assert "run_scenario" not in content
    assert "startLoadPlan" not in content


def test_generate_scenario_metadata_returns_fixed_error_contract():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "generate_scenario_metadata.groovy"
    content = script_path.read_text(encoding="utf-8")

    assert "error_detail:" not in content
    assert "error_chain:" not in content
    assert "root_error:" not in content
    assert "error_stack:" not in content
    assert "error_type: e.getClass().getSimpleName()" in content


def test_scenario_probe_blocks_without_explicit_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.scenario_probe(scenario_name="SCN_MAP_HR_EMPLOYEES_TO_ODS")

    assert result["status"] == "blocked_until_approval"
    assert result["repository_connection_attempted"] is False
    assert result["approval"]["action"] == "ODI_REPOSITORY_READ_ONLY"


def test_scenario_probe_uses_read_only_script(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "side_effects": False,
            "repository_connection_attempted": True,
            "checks": [{"name": "scenario_found", "status": "pass"}],
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.scenario_probe(
        approved=True,
        scenario_name="SCN_MAP_SH_SALES_TO_ODS",
        version="001",
    )

    assert result["status"] == "passed"
    assert result["side_effects"] is False
    assert calls == [
        (
            "scenario_probe.groovy",
            {
                "odi.scenario.name": "SCN_MAP_SH_SALES_TO_ODS",
                "odi.scenario.version": "001",
            },
        )
    ]


def test_scenario_probe_invalid_json_returns_structured_error(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "not json")

    result = orch.scenario_probe(
        approved=True,
        scenario_name="SCN_MAP_X",
    )

    checks = {item["name"]: item for item in result["checks"]}
    assert result["status"] == "error"
    assert result["side_effects"] is False
    assert checks["scenario_probe_json"]["status"] == "fail"


def test_scenario_probe_groovy_is_read_only_and_redacted():
    script_path = Path(__file__).resolve().parents[1] / "odi_automation" / "scenario_probe.groovy"
    content = script_path.read_text(encoding="utf-8")

    forbidden_tokens = [
        ".persist(",
        ".remove(",
        ".commit(",
        "generateScenario",
        "startScenario",
        "startLoadPlan",
        "new Mapping(",
    ]
    for token in forbidden_tokens:
        assert token not in content

    assert "findLatestByName" in content
    assert "getVersion()?.equalsIgnoreCase(version)" in content
    assert "findByUniqueName" not in content
    assert "replaceAll(/jdbc:" in content


def test_run_scenario_safe_blocks_without_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_scenario", lambda *_args, **_kwargs: "should not run")

    result = orch.run_scenario_safe(
        scenario_name="MAP_HR_EMPLOYEES_TO_ODS_SCN",
        version="V44A",
    )

    assert result["status"] == "blocked_until_approval"
    assert result["will_run_scenario"] is False
    assert result["repository_connection_attempted"] is False


def test_run_scenario_safe_blocks_non_allowlisted_scenario(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_scenario", lambda *_args, **_kwargs: "should not run")

    result = orch.run_scenario_safe(
        approved=True,
        scenario_name="MAP_UNKNOWN_TO_ODS_SCN",
        version="001",
    )

    assert result["status"] == "blocked_by_allowlist"
    assert result["will_run_scenario"] is False


def test_run_scenario_safe_returns_structured_success(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(
        orch,
        "run_scenario",
        lambda *_args, **_kwargs: "STEP 3: Senaryo tamamlandi\n  Session ID: 12345\n  Status: DONE\nSUCCESS",
    )

    result = orch.run_scenario_safe(
        approved=True,
        scenario_name="MAP_SH_SALES_TO_ODS_SCN",
        version="V45A",
    )

    assert result["status"] == "passed"
    assert result["will_run_scenario"] is True
    assert result["session"] == {"id": "12345", "status": "DONE"}
    assert "raw_output" not in result


def test_run_scenario_safe_redacts_failure_output(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(
        orch,
        "run_scenario",
        lambda *_args, **_kwargs: "ERROR: password=abc jdbc:oracle:thin:@host:1521/orcl failed",
    )

    result = orch.run_scenario_safe(
        approved=True,
        scenario_name="MAP_HR_EMPLOYEES_TO_ODS_SCN",
        version="V44A",
    )

    assert result["status"] == "failed"
    assert "abc" not in result["error_class"]
    assert "jdbc:oracle" not in result["error_class"]


def test_run_scenario_safe_prefers_meaningful_odi_error(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(
        orch,
        "run_scenario",
        lambda *_args, **_kwargs: (
            "Hata: Jun 19 log line\n"
            "INFO: noisy setup\n"
            "ODI-1217: Session MAP_SH_SALES_TO_ODS_SCN fails\n"
            "ORA-02019: connection description for remote database not found\n"
        ),
    )

    result = orch.run_scenario_safe(
        approved=True,
        scenario_name="MAP_SH_SALES_TO_ODS_SCN",
        version="V45A",
    )

    assert result["status"] == "failed"
    assert result["error_class"].startswith("ODI-1217")


def test_create_load_plan_metadata_blocks_without_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.create_load_plan_metadata(load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "blocked_until_approval"
    assert result["side_effects"] is False
    assert result["will_run_load_plan"] is False
    assert result["repository_connection_attempted"] is False


def test_create_load_plan_metadata_passes_allowlisted_steps(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({"status": "passed", "side_effects": True, "load_plan": {"name": "LP_HR_SH_ODS_LOAD"}})

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.create_load_plan_metadata(
        approved=True,
        load_plan_name="LP_HR_SH_ODS_LOAD",
        steps=[
            {"name": "Run HR Employees", "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN", "version": "V44A"},
            {"name": "Run SH Sales", "scenario_name": "MAP_SH_SALES_TO_ODS_SCN", "version": "V46B"},
        ],
    )

    assert result["status"] == "passed"
    assert calls == [
        (
            "create_load_plan_metadata.groovy",
            {
                "odi.load_plan.name": "LP_HR_SH_ODS_LOAD",
                "odi.context": "GLOBAL",
                "odi.steps": "Run HR Employees:MAP_HR_EMPLOYEES_TO_ODS_SCN:V44A,Run SH Sales:MAP_SH_SALES_TO_ODS_SCN:V46B",
            },
        )
    ]


def test_create_load_plan_metadata_rejects_non_allowlisted_scenario(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.create_load_plan_metadata(
        approved=True,
        load_plan_name="LP_BAD",
        steps=[{"name": "Bad", "scenario_name": "MAP_BAD_SCN", "version": "001"}],
    )

    assert result["status"] == "blocked_by_allowlist"
    assert result["side_effects"] is False
    assert result["will_run_load_plan"] is False


def test_load_plan_probe_blocks_without_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.load_plan_probe(load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "blocked_until_approval"
    assert result["side_effects"] is False
    assert result["repository_connection_attempted"] is False


def test_load_plan_probe_passes_params(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({"status": "passed", "side_effects": False, "load_plan": {"name": "LP_HR_SH_ODS_LOAD"}})

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)

    result = orch.load_plan_probe(approved=True, load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "passed"
    assert calls == [("load_plan_probe.groovy", {"odi.load_plan.name": "LP_HR_SH_ODS_LOAD"})]


def test_run_load_plan_safe_blocks_without_approval(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")

    result = orch.run_load_plan_safe(load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "blocked_until_approval"
    assert result["will_run_load_plan"] is False
    assert result["repository_connection_attempted"] is False


def test_run_load_plan_safe_blocks_non_allowlisted_plan(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_URL", "http://agent.example/oraclediagent")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_USER", "SUPERVISOR")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_PASSWORD", "secret")

    result = orch.run_load_plan_safe(approved=True, load_plan_name="LP_BAD")

    assert result["status"] == "blocked_by_allowlist"
    assert result["will_run_load_plan"] is False


def test_run_load_plan_safe_blocks_without_agent_config(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "should not run")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_URL", "")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_USER", "")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_PASSWORD", "")

    result = orch.run_load_plan_safe(approved=True, load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "blocked_until_agent_config"
    assert result["will_run_load_plan"] is False
    assert result["repository_connection_attempted"] is False
    assert result["checks"][0]["name"] == "odi_load_plan_agent_config"


def test_run_load_plan_safe_passes_allowlisted_params(monkeypatch):
    calls = []

    def fake_run(script_name, params):
        calls.append((script_name, params))
        return json.dumps({
            "status": "passed",
            "will_run_load_plan": True,
            "load_plan": {"name": "LP_HR_SH_ODS_LOAD"},
            "session": {"id": "201", "run_count": "1", "status": "DONE"},
        })

    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", fake_run)
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_URL", "http://agent.example/oraclediagent")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_USER", "SUPERVISOR")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_PASSWORD", "secret")

    result = orch.run_load_plan_safe(approved=True, load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "passed"
    assert result["will_run_load_plan"] is True
    assert result["session"] == {"id": "201", "run_count": "1", "status": "DONE"}
    assert calls == [
        (
            "run_load_plan_remote.groovy",
            {
                "odi.load_plan.name": "LP_HR_SH_ODS_LOAD",
                "odi.context": "GLOBAL",
            },
        )
    ]


def test_run_load_plan_safe_invalid_json_returns_structured_error(monkeypatch):
    orch = OdiOrchestrator()
    monkeypatch.setattr(orch, "run_groovy", lambda _script, _params: "not json")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_URL", "http://agent.example/oraclediagent")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_USER", "SUPERVISOR")
    monkeypatch.setattr(odi_orchestrator, "ODI_REST_PASSWORD", "secret")

    result = orch.run_load_plan_safe(approved=True, load_plan_name="LP_HR_SH_ODS_LOAD")

    assert result["status"] == "error"
    assert result["will_run_load_plan"] is True
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["run_load_plan_json"]["status"] == "fail"
