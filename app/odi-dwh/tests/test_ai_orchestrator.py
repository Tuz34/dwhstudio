"""AI orchestrator safety contract tests."""

import json

from ai_orchestrator import (
    _approval_matrix,
    _build_approval_plan,
    _finalize_evidence_pack,
    _odi_dry_run_mapping,
    _odi_read_only_preflight,
    _oracle_load_strategy,
    orchestrate,
)


def test_evidence_pack_v2_removes_rows_and_redacts_secrets():
    pack = {
        "job_id": "job-1",
        "task_type": "ods_modeling",
        "rows": [{"EMAIL": "real@example.com"}],
        "local_findings": [
            "password=SuperSecret123",
            "token: abcdefghijklmnop",
        ],
        "columns": [
            {"name": "EMAIL", "type": "VARCHAR2(25)", "comment": "Email id"},
            {"name": "PHONE_NUMBER", "type": "VARCHAR2(20)", "comment": "Phone"},
            {"name": "SALARY", "type": "NUMBER(8,2)", "comment": "Monthly salary"},
        ],
    }

    safe = _finalize_evidence_pack(pack)
    payload = json.dumps(safe, ensure_ascii=False)

    assert safe["evidence_pack_version"] == 2
    assert "rows" not in safe
    assert "real@example.com" not in payload
    assert "SuperSecret123" not in payload
    assert "abcdefghijklmnop" not in payload
    assert safe["redaction_report"]["removed_keys"] == ["rows"]
    assert safe["redaction_report"]["redactions"]
    assert safe["sensitivity_summary"]["pii_or_sensitive_columns"]["EMAIL"] == ["email"]
    assert "phone" in safe["sensitivity_summary"]["pii_or_sensitive_columns"]["PHONE_NUMBER"]
    assert "financial" in safe["sensitivity_summary"]["pii_or_sensitive_columns"]["SALARY"]


def test_orchestrate_outputs_evidence_pack_v2_for_supported_prompt():
    result = orchestrate("HR.EMPLOYEES tablosunu ODS katmanina dry-run modelle")
    evidence = result["evidence_pack"]
    checks = {item["check"]: item["ok"] for item in result["test_plan"] if "check" in item}

    assert evidence["evidence_pack_version"] == 2
    assert evidence["privacy_policy"]["raw_rows"] == "forbidden"
    assert "sensitivity_summary" in evidence
    assert checks["evidence_pack_v2"] is True
    assert checks["no_blocked_keys_in_evidence"] is True
    assert checks["no_secret_patterns_in_evidence"] is True


def test_orchestrate_outputs_observability_contract():
    result = orchestrate("HR.EMPLOYEES tablosunu ODS katmanina dry-run modelle")
    observability = result["observability"]
    node_metrics = {item["node"]: item for item in observability["node_metrics"]}

    assert observability["status"] == "SUCCEEDED"
    assert observability["job_id"] == result["job_id"]
    assert observability["duration_ms"] is not None
    assert "runtime_status" in observability["route"]
    assert "local_openai_models" in observability["model_runtime"]
    assert "ollama_models" in observability["model_runtime"]
    assert "gpu_before" in observability
    assert "gpu_after" in observability
    assert node_metrics["init"]["duration_ms"] >= 0
    assert node_metrics["planner"]["status"] == "SUCCESS"
    assert node_metrics["final_composer"]["status"] == "SUCCESS"


def test_odi_dry_run_mapping_passes_for_logical_contract():
    result = _odi_dry_run_mapping({
        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        "source_table": "HR.EMPLOYEES",
        "target_table": "DWH.ODS_HR_EMPLOYEES",
        "target_layer": "ODS",
        "columns": [
            {"name": "EMPLOYEE_ID", "type": "NUMBER"},
            {"name": "HIRE_DATE", "type": "DATE"},
        ],
        "pk_columns": ["EMPLOYEE_ID"],
        "oracle_load_strategy": {
            "strategy": "full_load_then_merge_by_pk",
            "recommended_odi_km": "LKM Oracle to Oracle + IKM Oracle Incremental Update",
            "approval_required": ["LIVE_ODI_CREATE"],
        },
    })

    checks = {item["name"]: item for item in result["checks"]}

    assert result["passed"] is True
    assert result["side_effects"] is False
    assert result["mode"] == "logical_only"
    assert checks["side_effects_disabled"]["status"] == "pass"
    assert checks["pk_columns_in_source"]["status"] == "pass"
    assert checks["live_odi_approval_required"]["status"] == "pass"


def test_odi_dry_run_mapping_fails_missing_columns_and_bad_target():
    result = _odi_dry_run_mapping({
        "source_table": "EMPLOYEES",
        "target_table": "ODS_HR_EMPLOYEES",
        "target_layer": "ODS",
        "columns": [],
        "pk_columns": ["EMPLOYEE_ID"],
        "oracle_load_strategy": {},
    })
    checks = {item["name"]: item for item in result["checks"]}

    assert result["passed"] is False
    assert checks["source_table_qualified"]["status"] == "fail"
    assert checks["target_table_qualified"]["status"] == "fail"
    assert checks["source_columns_present"]["status"] == "fail"
    assert checks["load_strategy_selected"]["status"] == "fail"


def test_orchestrate_outputs_odi_dry_run_contract(monkeypatch):
    import ai_orchestrator

    def summarize_catalog(table: str):
        assert table == "HR.EMPLOYEES"
        return (
            [
                {"name": "EMPLOYEE_ID", "type": "NUMBER", "nullable": "N", "comment": ""},
                {"name": "HIRE_DATE", "type": "DATE", "nullable": "Y", "comment": ""},
            ],
            ["EMPLOYEE_ID"],
            [],
        )

    monkeypatch.setattr(ai_orchestrator, "_summarize_columns", summarize_catalog)

    result = orchestrate("HR.EMPLOYEES tablosunu ODS katmanina dry-run modelle")
    checks = {item["check"]: item["ok"] for item in result["test_plan"] if "check" in item}
    gates = {item["gate"]: item for item in result["quality_gates"]}
    column_names = [column["name"] for column in result["evidence_pack"]["columns"]]
    assert column_names[:2] == ["EMPLOYEE_ID", "HIRE_DATE"]

    assert result["odi_dry_run"]["tool"] == "odi.dry_run_mapping"
    assert result["odi_dry_run"]["side_effects"] is False
    assert result["odi_dry_run"]["passed"] is True
    assert result["evidence_pack"]["odi_dry_run"]["mode"] == "logical_only"
    assert checks["odi_dry_run_side_effect_free"] is True
    assert checks["odi_dry_run_passed"] is True
    assert gates["odi_dry_run_mapping"]["status"] == "pass"


def test_odi_read_only_preflight_blocks_without_approval():
    mapping_spec = {
        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        "source_table": "HR.EMPLOYEES",
        "target_table": "DWH.ODS_HR_EMPLOYEES",
    }
    plan = _build_approval_plan({
        "prompt": "HR.EMPLOYEES ODS modelle",
        "task_type": "ods_modeling",
        "dry_run": True,
        "odi_repository_read_only_requested": True,
    })

    preflight = _odi_read_only_preflight(mapping_spec, plan)

    assert preflight["status"] == "blocked_until_approval"
    assert preflight["ready"] is False
    assert preflight["side_effects"] is False
    assert preflight["repository_connection_attempted"] is False
    assert "create" in preflight["forbidden_operations"]
    assert preflight["approval"]["action"] == "ODI_REPOSITORY_READ_ONLY"


def test_odi_read_only_preflight_ready_with_explicit_approval():
    mapping_spec = {
        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        "source_table": "HR.EMPLOYEES",
        "target_table": "DWH.ODS_HR_EMPLOYEES",
    }
    plan = _build_approval_plan(
        {
            "prompt": "HR.EMPLOYEES ODS modelle",
            "task_type": "ods_modeling",
            "dry_run": True,
            "odi_repository_read_only_requested": True,
        },
        approved_actions=["ODI_REPOSITORY_READ_ONLY"],
    )

    preflight = _odi_read_only_preflight(mapping_spec, plan)

    assert preflight["status"] == "ready_for_read_only_connection"
    assert preflight["ready"] is True
    assert preflight["approval"]["status"] == "approved_for_current_run"
    assert preflight["approval"]["auto_approved"] is False


def test_orchestrate_outputs_odi_read_only_preflight_contract():
    result = orchestrate("HR.EMPLOYEES tablosunu ODS katmanina dry-run modelle")
    checks = {item["check"]: item["ok"] for item in result["test_plan"] if "check" in item}
    actions = {gate["action"]: gate for gate in result["approval_plan"]["required_actions"]}

    assert result["odi_read_only_preflight"]["tool"] == "odi.read_only_preflight"
    assert result["odi_read_only_preflight"]["status"] == "blocked_until_approval"
    assert result["odi_read_only_preflight"]["repository_connection_attempted"] is False
    assert result["evidence_pack"]["odi_read_only_preflight"]["mode"] == "connectionless_preflight"
    assert actions["ODI_REPOSITORY_READ_ONLY"]["decision"] == "blocked_until_user_approval"
    assert checks["odi_read_only_preflight_connectionless"] is True
    assert checks["odi_read_only_preflight_status_valid"] is True


def test_orchestrate_read_only_preflight_ready_when_approved():
    result = orchestrate(
        "HR.EMPLOYEES tablosunu ODS katmanina dry-run modelle",
        approved_actions=["ODI_REPOSITORY_READ_ONLY"],
    )

    assert result["odi_read_only_preflight"]["status"] == "ready_for_read_only_connection"
    assert result["odi_read_only_preflight"]["ready"] is True
    assert result["approval_plan"]["summary"]["approved_count"] == 1


def test_approval_matrix_has_role_based_safety_gates():
    matrix = _approval_matrix()

    assert set(matrix) >= {
        "DDL",
        "ODI_CREATE_MAPPING",
        "ODI_REPOSITORY_READ_ONLY",
        "DROP_OR_ALTER_TARGET",
        "CLOUD_EVIDENCE_SEND",
        "CREDENTIAL_OR_ENV_CHANGE",
    }
    assert "DATA_ENGINEER" in matrix["ODI_CREATE_MAPPING"]["required_roles"]
    assert "DWH_ARCHITECT" in matrix["DDL"]["required_roles"]
    assert "ADMIN" in matrix["DROP_OR_ALTER_TARGET"]["required_roles"]
    assert matrix["CREDENTIAL_OR_ENV_CHANGE"]["denial_result"] == "do_not_modify_configuration"


def test_build_approval_plan_requires_live_gates_without_auto_approval():
    plan = _build_approval_plan(
        {
            "prompt": "HR.EMPLOYEES ODS modelle ve cloud review yap",
            "task_type": "ods_modeling",
            "dry_run": True,
            "allow_cloud_review": True,
        },
        load_strategy={"approval_required": ["LIVE_ODI_CREATE"]},
        cloud_steps=[{"step": "cloud_review", "enabled": True, "input": "evidence_pack_only"}],
    )
    actions = {gate["action"]: gate for gate in plan["required_actions"]}

    assert plan["version"] == 1
    assert actions["DDL"]["decision"] == "blocked_until_user_approval"
    assert actions["ODI_CREATE_MAPPING"]["decision"] == "blocked_until_user_approval"
    assert actions["CLOUD_EVIDENCE_SEND"]["decision"] == "blocked_until_user_approval"
    assert all(gate["auto_approved"] is False for gate in plan["gates"])


def test_orchestrate_outputs_approval_plan_contract():
    result = orchestrate("HR.EMPLOYEES tablosunu ODS katmanina dry-run modelle")
    plan = result["approval_plan"]
    checks = {item["check"]: item["ok"] for item in result["test_plan"] if "check" in item}
    actions = {gate["action"]: gate for gate in plan["required_actions"]}

    assert result["approval_required_for"] == [
        "DDL",
        "ODI_CREATE_MAPPING",
        "DROP_OR_ALTER_TARGET",
        "CLOUD_EVIDENCE_SEND",
        "CREDENTIAL_OR_ENV_CHANGE",
    ]
    assert plan["version"] == 1
    assert actions["DDL"]["status"] == "required_before_live_execution"
    assert actions["ODI_CREATE_MAPPING"]["status"] == "required_before_live_execution"
    assert plan["summary"]["auto_approved_count"] == 0
    assert "DWH_ARCHITECT" in plan["summary"]["required_roles"]
    assert result["evidence_pack"]["approval_plan"]["version"] == 1
    assert checks["approval_matrix_v1"] is True
    assert checks["no_auto_approved_live_gate"] is True
    assert checks["approval_roles_present"] is True


def test_restricted_prompt_blocks_cloud_even_when_requested():
    result = orchestrate(
        "HR.EMPLOYEES ODS modelle password=Secret123",
        allow_cloud_review=True,
    )
    checks = {item["check"]: item["ok"] for item in result["test_plan"] if "check" in item}
    required_actions = {item["action"] for item in result["approval_plan"]["required_actions"]}

    assert result["privacy_level"] == "restricted"
    assert all(not step.get("enabled") for step in result["cloud_steps"])
    assert checks["restricted_blocks_cloud"] is True
    assert checks["no_secret_patterns_in_evidence"] is True
    assert "CLOUD_EVIDENCE_SEND" in required_actions
    assert "CREDENTIAL_OR_ENV_CHANGE" in required_actions


def test_oracle_load_strategy_uses_pk_without_watermark_as_cdc_design_needed():
    strategy = _oracle_load_strategy(
        "HR.EMPLOYEES",
        [
            {"name": "EMPLOYEE_ID", "type": "NUMBER"},
            {"name": "HIRE_DATE", "type": "DATE"},
        ],
        ["EMPLOYEE_ID"],
    )

    assert strategy["strategy"] == "full_load_then_merge_by_pk"
    assert strategy["merge_key"] == ["EMPLOYEE_ID"]
    assert "CDC_DESIGN" in strategy["approval_required"]


def test_oracle_load_strategy_detects_fact_watermark_without_pk():
    strategy = _oracle_load_strategy(
        "SH.SALES",
        [
            {"name": "PROD_ID", "type": "NUMBER"},
            {"name": "CUST_ID", "type": "NUMBER"},
            {"name": "TIME_ID", "type": "DATE"},
            {"name": "AMOUNT_SOLD", "type": "NUMBER"},
        ],
        [],
    )

    assert strategy["strategy"] == "incremental_append_with_watermark"
    assert strategy["watermark_column"] == "TIME_ID"
    assert "FACT_GRAIN_CONFIRMATION" in strategy["approval_required"]
