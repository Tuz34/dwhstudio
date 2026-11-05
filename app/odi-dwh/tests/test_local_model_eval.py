import json
import sys

import pytest

import local_model_eval as evals


def test_golden_cases_v1_have_required_counts_and_static_safety_boundary():
    assert set(evals.GOLDEN_CASES) == {
        "nl_to_sql",
        "intent_to_json",
        "error_to_fix",
        "evidence_summary",
    }

    all_ids = []
    for task, cases in evals.GOLDEN_CASES.items():
        assert 15 <= len(cases) <= 30, task
        for case in cases:
            all_ids.append(case["id"])
            assert case["task"] == task
            assert case["synthetic"] is True
            assert case["validation_mode"] == "static_offline"
            assert case["live_execution_allowed"] is False
            assert case["model_call_allowed"] is False
            assert case["expected"]
            evals.assert_no_blocked_content(case)

    assert len(all_ids) == len(set(all_ids))


def test_static_eval_runner_scores_reference_outputs_without_live_execution():
    report = evals.run_static_eval()

    assert report["mode"] == "static_offline"
    assert report["live_execution_attempted"] is False
    assert report["model_call_attempted"] is False
    assert report["overall"]["total"] == 60
    assert report["overall"]["passed"] == 60
    assert report["overall"]["score"] == 1.0
    assert report["thresholds"]["nl_to_sql"] == 0.80
    assert report["thresholds"]["intent_to_json"] == 0.95
    assert report["thresholds"]["error_to_fix"] == 0.80
    assert report["thresholds"]["evidence_summary"] == 0.90
    assert all(task["verdict"] == "static_reference_passes" for task in report["tasks"].values())


def test_sql_validator_rejects_write_keywords_and_unknown_schema_references():
    case = evals.case_by_id("sql_hr_employee_count_by_department")

    write_result = evals.validate_case(case, "DELETE FROM DWH.ODS_HR_EMPLOYEES")
    assert write_result.passed is False
    assert "forbidden_keyword" in write_result.reasons

    unknown_result = evals.validate_case(case, "SELECT COUNT(*) FROM FIN.PAYROLL")
    assert unknown_result.passed is False
    assert "unknown_table" in unknown_result.reasons


def test_intent_json_validator_rejects_missing_required_fields_and_mismatch():
    case = evals.case_by_id("json_create_hr_employees_ods")

    missing = evals.validate_case(case, {"operation": "CREATE"})
    assert missing.passed is False
    assert "missing_required_field" in missing.reasons

    mismatched = dict(case["expected"]["output"])
    mismatched["tgt_table"] = "ODS_WRONG"
    mismatch_result = evals.validate_case(case, json.dumps(mismatched))
    assert mismatch_result.passed is False
    assert "expected_value_mismatch" in mismatch_result.reasons


def test_intent_json_validator_treats_description_and_null_optional_fields_as_semantic():
    case = evals.case_by_id("json_create_hr_employees_ods")
    output = dict(case["expected"]["output"])
    output.pop("skey_natural_key")
    output["description"] = "Create the requested HR employees ODS mapping."

    result = evals.validate_case(case, output)

    assert result.passed is True


def test_error_fix_validator_requires_root_cause_code_and_next_step_keywords():
    case = evals.case_by_id("err_odi_16171_missing_update_key")

    wrong = evals.validate_case(
        case,
        {
            "root_cause_code": "ORA-01031",
            "next_steps": ["Grant broader privileges"],
        },
    )
    assert wrong.passed is False
    assert "root_cause_mismatch" in wrong.reasons

    incomplete = evals.validate_case(
        case,
        {
            "root_cause_code": case["expected"]["root_cause_code"],
            "next_steps": ["Retry the mapping"],
        },
    )
    assert incomplete.passed is False
    assert "missing_next_step_keyword" in incomplete.reasons


def test_summary_validator_hard_fails_secret_and_raw_row_leaks():
    case = evals.case_by_id("sum_schema_sync_findings")

    secret_result = evals.validate_case(case, "Everything passed. PASSWORD=not-for-output")
    assert secret_result.passed is False
    assert "blocked_content" in secret_result.reasons

    raw_row_result = evals.validate_case(case, "rows=[{'ID': 1, 'NAME': 'example'}]")
    assert raw_row_result.passed is False
    assert "blocked_content" in raw_row_result.reasons


def test_summary_validator_accepts_narrow_semantic_keyword_aliases():
    local_llm_case = evals.case_by_id("sum_local_llm_guard")
    local_llm_result = evals.validate_case(
        local_llm_case,
        "Local LLM analysis used redacted text and did not unload a model it did not load.",
    )

    model_eval_case = evals.case_by_id("sum_model_eval_plan")
    model_eval_result = evals.validate_case(
        model_eval_case,
        "The model plan starts with evaluations, then RAG and constrained decoding before optional fine-tuning.",
    )

    error_pattern_case = evals.case_by_id("sum_error_pattern_memory")
    error_pattern_result = evals.validate_case(
        error_pattern_case,
        "Error pattern memory stores redacted troubleshooting patterns and prevents exposing secrets, connection strings, and raw rows.",
    )

    assert local_llm_result.passed is True
    assert model_eval_result.passed is True
    assert error_pattern_result.passed is True


def test_runner_accepts_prediction_overrides_and_records_threshold_status():
    bad_outputs = {
        "sql_hr_employee_count_by_department": "DROP TABLE DWH.ODS_HR_EMPLOYEES",
        "sum_schema_sync_findings": "PASSWORD=not-for-output",
    }

    report = evals.run_static_eval(bad_outputs)

    assert report["tasks"]["nl_to_sql"]["passed"] == 14
    assert report["tasks"]["nl_to_sql"]["meets_threshold"] is True
    assert report["tasks"]["evidence_summary"]["passed"] == 14
    assert report["tasks"]["evidence_summary"]["meets_threshold"] is True
    assert report["overall"]["passed"] == 58


def test_loading_predictions_file_scores_case_outputs(tmp_path):
    predictions_path = tmp_path / "predictions.json"
    predictions_path.write_text(
        json.dumps(
            {
                "outputs": {
                    "json_create_hr_employees_ods": {"operation": "CREATE"},
                }
            }
        ),
        encoding="utf-8",
    )

    report = evals.run_static_eval(evals.load_predictions(predictions_path))

    assert report["tasks"]["intent_to_json"]["passed"] == 14
    assert report["tasks"]["intent_to_json"]["meets_threshold"] is False


def test_guarded_json_normalization_unwraps_fenced_json_for_json_tasks():
    case = evals.case_by_id("json_create_hr_employees_ods")
    predictions = {
        case["id"]: "```json\n" + json.dumps(case["expected"]["output"]) + "\n```",
        "sql_hr_employee_count_by_department": "```sql\nSELECT COUNT(*) FROM HR.EMPLOYEES\n```",
    }

    result = evals.normalize_predictions_with_prompt_guard(predictions)

    assert result["summary"] == {
        "mode": "guarded_json_normalization",
        "eligible": 1,
        "normalized": 1,
        "unchanged": 1,
        "blocked": 0,
    }
    assert result["outputs"][case["id"]] == case["expected"]["output"]
    assert result["outputs"]["sql_hr_employee_count_by_department"] == predictions["sql_hr_employee_count_by_department"]


def test_guarded_json_normalization_rejects_blocked_content_before_normalizing():
    case = evals.case_by_id("json_create_hr_employees_ods")

    with pytest.raises(ValueError):
        evals.normalize_predictions_with_prompt_guard(
            {case["id"]: "```json\n{\"operation\":\"CREATE\"}\n```\nPASSWORD=not-for-output"}
        )


def test_intent_semantic_repair_is_narrow_for_product_dimension_skey_case():
    case = evals.case_by_id("json_create_dim_product_skey")
    model_output = {
        "operation": "CREATE",
        "mapping_name": "MAP_SH_PRODUCTS_TO_ODS",
        "src_model": "ORACLE_SH",
        "src_table": "SH.PRODUCTS",
        "tgt_model": "DWH",
        "tgt_table": "DIM_PRODUCT",
        "tgt_owner": "DWH",
        "needs_skey": True,
        "description": "Mapping for SH products to DIM_PRODUCT with surrogate key based on SRC.PROD_ID",
    }

    assert evals.validate_case(case, model_output).passed is False

    result = evals.repair_intent_outputs_with_prompt_guard({case["id"]: model_output})

    assert result["summary"] == {
        "mode": "intent_prompt_value_repair",
        "eligible": 1,
        "repaired": 1,
        "unchanged": 0,
        "blocked": 0,
    }
    repaired = result["outputs"][case["id"]]
    assert repaired["mapping_name"] == "MAP_SH_PRODUCTS_TO_DIM"
    assert repaired["skey_natural_key"] == "SRC.PROD_ID"
    assert evals.validate_case(case, repaired).passed is True


def test_intent_schema_guard_unwraps_single_result_wrapper_only():
    case = evals.case_by_id("json_create_hr_employees_ods")
    wrapped_output = {"result": case["expected"]["output"]}

    assert evals.validate_case(case, wrapped_output).passed is False

    result = evals.apply_intent_schema_guard({case["id"]: wrapped_output})

    assert result["summary"] == {
        "mode": "intent_schema_guard",
        "eligible": 1,
        "unwrapped": 1,
        "unchanged": 0,
        "blocked": 0,
    }
    assert result["outputs"][case["id"]] == case["expected"]["output"]
    assert evals.validate_case(case, result["outputs"][case["id"]]).passed is True


def test_intent_schema_guard_refuses_ambiguous_wrapper_with_sibling_text():
    case = evals.case_by_id("json_create_hr_employees_ods")
    ambiguous_output = {
        "result": case["expected"]["output"],
        "explanation": "This wrapper has sibling text, so it is not a pure JSON envelope.",
    }

    result = evals.apply_intent_schema_guard({case["id"]: ambiguous_output})

    assert result["summary"] == {
        "mode": "intent_schema_guard",
        "eligible": 1,
        "unwrapped": 0,
        "unchanged": 1,
        "blocked": 0,
    }
    assert result["outputs"][case["id"]] == ambiguous_output
    assert evals.validate_case(case, result["outputs"][case["id"]]).passed is False


def test_cli_can_score_predictions_with_guarded_json_normalization(tmp_path, monkeypatch):
    case = evals.case_by_id("json_create_hr_employees_ods")
    predictions_path = tmp_path / "predictions.json"
    report_path = tmp_path / "report.json"
    predictions_path.write_text(
        json.dumps({"outputs": {case["id"]: "```json\n" + json.dumps(case["expected"]["output"]) + "\n```"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--predictions-file",
            str(predictions_path),
            "--guarded-json-normalize",
            "--out",
            str(report_path),
        ],
    )

    exit_code = evals.main()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["prediction_guard"]["normalized"] == 1
    assert report["tasks"]["intent_to_json"]["passed"] == 15
    assert report["model_call_attempted"] is False
    assert report["live_execution_attempted"] is False


def test_cli_can_score_predictions_with_intent_schema_guard(tmp_path, monkeypatch):
    case = evals.case_by_id("json_create_hr_employees_ods")
    predictions_path = tmp_path / "predictions.json"
    report_path = tmp_path / "report.json"
    predictions_path.write_text(
        json.dumps({"outputs": {case["id"]: {"result": case["expected"]["output"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--predictions-file",
            str(predictions_path),
            "--intent-schema-guard",
            "--out",
            str(report_path),
        ],
    )

    exit_code = evals.main()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["prediction_guards"][0]["mode"] == "intent_schema_guard"
    assert report["prediction_guards"][0]["unwrapped"] == 1
    assert report["tasks"]["intent_to_json"]["passed"] == 15
    assert report["model_call_attempted"] is False
    assert report["live_execution_attempted"] is False


def test_cli_can_score_predictions_with_intent_semantic_repair(tmp_path, monkeypatch):
    case = evals.case_by_id("json_create_dim_product_skey")
    predictions_path = tmp_path / "predictions.json"
    report_path = tmp_path / "report.json"
    predictions_path.write_text(
        json.dumps(
            {
                "outputs": {
                    case["id"]: {
                        "operation": "CREATE",
                        "mapping_name": "MAP_SH_PRODUCTS_TO_ODS",
                        "src_model": "ORACLE_SH",
                        "src_table": "SH.PRODUCTS",
                        "tgt_model": "DWH",
                        "tgt_table": "DIM_PRODUCT",
                        "tgt_owner": "DWH",
                        "needs_skey": True,
                        "description": "Mapping for SH products to DIM_PRODUCT with surrogate key based on SRC.PROD_ID",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--predictions-file",
            str(predictions_path),
            "--intent-semantic-repair",
            "--out",
            str(report_path),
        ],
    )

    exit_code = evals.main()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["prediction_guards"][0]["mode"] == "intent_prompt_value_repair"
    assert report["prediction_guards"][0]["repaired"] == 1
    assert report["tasks"]["intent_to_json"]["passed"] == 15
    assert report["model_call_attempted"] is False
    assert report["live_execution_attempted"] is False


def test_prompt_pack_exports_all_cases_without_expected_answers():
    pack = evals.build_prompt_pack()

    assert pack["mode"] == "static_prompt_pack"
    assert pack["total"] == 60
    assert pack["model_call_attempted"] is False
    assert pack["live_execution_attempted"] is False

    payload = json.dumps(pack, ensure_ascii=False)
    assert '"expected"' not in payload
    assert '"output"' not in payload
    assert "DROP TABLE" not in payload

    prompts = {item["id"]: item for item in pack["prompts"]}
    assert set(prompts) == {case["id"] for cases in evals.GOLDEN_CASES.values() for case in cases}
    assert prompts["json_create_hr_employees_ods"]["task"] == "intent_to_json"
    assert "Return exactly one JSON object" in prompts["json_create_hr_employees_ods"]["prompt"]
    assert "Return only Oracle SQL" in prompts["sql_hr_employee_count_by_department"]["prompt"]


def test_prompt_pack_includes_json_schema_contracts_without_golden_values():
    prompts = {item["id"]: item for item in evals.build_prompt_pack()["prompts"]}
    intent_prompt = prompts["json_create_hr_employees_ods"]["prompt"]
    error_prompt = prompts["err_ora_01031_insufficient_privileges"]["prompt"]
    summary_prompt = prompts["sum_schema_sync_findings"]["prompt"]

    assert "Return exactly one JSON object" in intent_prompt
    assert '"operation"' in intent_prompt
    assert '"mapping_name"' in intent_prompt
    assert '"changes"' in intent_prompt
    assert "Do not add wrapper keys" in intent_prompt
    assert "Mapping name convention" in intent_prompt
    assert "MAP_<SOURCE_SCHEMA>_<SOURCE_TABLE>_TO_ODS" in intent_prompt
    assert "Source model convention" in intent_prompt
    assert "ORACLE_<SOURCE_SCHEMA>" in intent_prompt
    assert "Use pk_parity for PK parity validation" in intent_prompt
    assert "Do not infer a surrogate key for ordinary ODS mappings" in intent_prompt
    assert "For UPDATE requests, put only the requested changed fields under changes" in intent_prompt
    assert "Use tgt_table for target table rename/change requests" in intent_prompt
    assert "Use truncate_target_table false when the task asks to disable truncate" in intent_prompt
    assert "Use filter_condition for requested filter predicates" in intent_prompt
    assert "MAP_HR_EMPLOYEES_TO_ODS" not in intent_prompt
    assert "ODS_HR_EMPLOYEES_V2" not in intent_prompt
    assert "SRC.DEPARTMENT_ID IS NOT NULL" not in intent_prompt

    assert "Return exactly one JSON object" in error_prompt
    assert '"root_cause_code"' in error_prompt
    assert '"next_steps"' in error_prompt
    assert "Remediation playbook" in error_prompt
    assert "ODI-16171" in error_prompt
    assert "target key" in error_prompt
    assert "ORA-02019" in error_prompt
    assert "database link" in error_prompt
    assert "next_step_keywords" not in error_prompt
    assert "expected" not in error_prompt.lower()

    assert "Include the workflow name" in summary_prompt
    assert "Schema Sync" in summary_prompt
    assert "metadata differences" in summary_prompt
    assert "type and length mismatches" in summary_prompt
    assert "Do not invent row-level details" in summary_prompt
    assert evals.case_by_id("sum_schema_sync_findings")["expected"]["output"] not in summary_prompt


def test_intent_prompt_pack_includes_user_requested_values_for_structured_changes():
    prompts = {item["id"]: item["prompt"] for item in evals.build_prompt_pack()["prompts"]}

    assert "target table to ODS_HR_EMPLOYEES_V2" in prompts["json_update_hr_employees_target_table"]
    assert "source filter SRC.DEPARTMENT_ID IS NOT NULL" in prompts["json_update_hr_departments_filter"]
    assert "target dimension table DIM_PRODUCT" in prompts["json_create_dim_product_skey"]
    assert "surrogate key natural key SRC.PROD_ID" in prompts["json_create_dim_product_skey"]
    assert "not an ODS mapping" in prompts["json_create_dim_product_skey"]
    assert "MAP_SH_PRODUCTS_TO_DIM" not in prompts["json_create_dim_product_skey"]


def test_export_prompt_pack_writes_prediction_input_template(tmp_path):
    path = tmp_path / "prompt_pack.json"

    result = evals.export_prompt_pack(path)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert result["status"] == "written"
    assert result["path"] == str(path)
    assert saved["total"] == 60
    assert "predictions_file_schema" in saved
    assert saved["predictions_file_schema"]["top_level_key"] == "outputs"


def test_intent_json_schema_contract_is_static_and_has_no_golden_values():
    artifact = evals.build_intent_json_schema_contract()

    assert artifact["mode"] == "intent_json_schema_contract"
    assert artifact["task"] == "intent_to_json"
    assert artifact["model_call_attempted"] is False
    assert artifact["live_execution_attempted"] is False
    assert artifact["schema"]["oneOf"]

    payload = json.dumps(artifact, ensure_ascii=False)
    evals.assert_no_blocked_content(artifact)
    assert '"expected"' not in payload
    assert '"output"' not in payload
    assert "MAP_HR_EMPLOYEES_TO_ODS" not in payload
    assert "ODS_HR_EMPLOYEES_V2" not in payload
    assert "SRC.DEPARTMENT_ID IS NOT NULL" not in payload

    variants = {variant["properties"]["operation"]["const"]: variant for variant in artifact["schema"]["oneOf"]}
    assert set(variants) == {"CREATE", "UPDATE", "DELETE", "VALIDATE"}
    assert set(variants["CREATE"]["required"]) == set(evals._required_json_fields("CREATE"))
    assert set(variants["UPDATE"]["properties"]["changes"]["properties"]) == {
        "tgt_table",
        "truncate_target_table",
        "filter_condition",
        "tgt_owner",
    }
    assert variants["UPDATE"]["properties"]["changes"]["minProperties"] == 1


def test_export_intent_json_schema_contract_writes_offline_artifact(tmp_path):
    path = tmp_path / "intent_schema.json"

    result = evals.export_intent_json_schema_contract(path)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert result == {"status": "written", "path": str(path)}
    assert saved["mode"] == "intent_json_schema_contract"
    assert saved["model_call_attempted"] is False
    assert saved["live_execution_attempted"] is False


def test_cli_can_write_intent_json_schema_contract_without_model_calls(tmp_path, monkeypatch):
    schema_path = tmp_path / "intent_schema.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--intent-schema-out",
            str(schema_path),
        ],
    )

    exit_code = evals.main()
    saved = json.loads(schema_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert saved["mode"] == "intent_json_schema_contract"
    assert saved["model_call_attempted"] is False
    assert saved["live_execution_attempted"] is False


def test_intent_gbnf_grammar_contract_is_static_and_schema_derived():
    schema_artifact = evals.build_intent_json_schema_contract()
    grammar_artifact = evals.build_intent_gbnf_grammar_contract()

    assert grammar_artifact["mode"] == "intent_gbnf_grammar_contract"
    assert grammar_artifact["task"] == "intent_to_json"
    assert grammar_artifact["source_contract_mode"] == schema_artifact["mode"]
    assert grammar_artifact["model_call_attempted"] is False
    assert grammar_artifact["live_execution_attempted"] is False
    assert grammar_artifact["format"] == "gbnf"

    schema_operations = [
        variant["properties"]["operation"]["const"]
        for variant in schema_artifact["schema"]["oneOf"]
    ]
    assert grammar_artifact["supported_operations"] == schema_operations
    assert 'root ::= create | update | delete | validate' in grammar_artifact["grammar"]
    assert '\\"CREATE\\"' in grammar_artifact["grammar"]
    assert '\\"VALIDATE\\"' in grammar_artifact["grammar"]

    payload = json.dumps(grammar_artifact, ensure_ascii=False)
    evals.assert_no_blocked_content(grammar_artifact)
    assert '"expected"' not in payload
    assert '"output"' not in payload
    assert "MAP_HR_EMPLOYEES_TO_ODS" not in payload
    assert "ODS_HR_EMPLOYEES_V2" not in payload
    assert "SRC.DEPARTMENT_ID IS NOT NULL" not in payload


def test_export_intent_gbnf_grammar_contract_writes_offline_artifact(tmp_path):
    path = tmp_path / "intent_grammar.gbnf.json"

    result = evals.export_intent_gbnf_grammar_contract(path)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert result == {"status": "written", "path": str(path)}
    assert saved["mode"] == "intent_gbnf_grammar_contract"
    assert saved["model_call_attempted"] is False
    assert saved["live_execution_attempted"] is False
    assert saved["grammar"].strip().startswith("root ::=")


def test_cli_can_write_intent_gbnf_grammar_without_model_calls(tmp_path, monkeypatch):
    grammar_path = tmp_path / "intent_grammar.gbnf.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--intent-grammar-out",
            str(grammar_path),
        ],
    )

    exit_code = evals.main()
    saved = json.loads(grammar_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert saved["mode"] == "intent_gbnf_grammar_contract"
    assert saved["model_call_attempted"] is False
    assert saved["live_execution_attempted"] is False


def test_structured_output_runbook_documents_offline_boundaries():
    path = evals.Path("docs/local-model-structured-output.md")
    text = path.read_text(encoding="utf-8")

    assert "No GPU use" in text
    assert "No model load or inference" in text
    assert "No DB or ODI action" in text
    assert "--intent-schema-out" in text
    assert "--intent-grammar-out" in text
    assert "RAG semantic planning" in text
    assert "live-proven" not in text.lower()
    evals.assert_no_blocked_content(text)


def test_cli_can_write_static_report_and_prompt_pack_in_one_run(tmp_path, monkeypatch):
    report_path = tmp_path / "static_report.json"
    prompt_path = tmp_path / "prompt_pack.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--out",
            str(report_path),
            "--prompts-out",
            str(prompt_path),
        ],
    )

    exit_code = evals.main()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    prompt_pack = json.loads(prompt_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["mode"] == "static_offline"
    assert report["model_call_attempted"] is False
    assert report["live_execution_attempted"] is False
    assert prompt_pack["mode"] == "static_prompt_pack"
    assert prompt_pack["model_call_attempted"] is False
    assert prompt_pack["live_execution_attempted"] is False


class _FakeResponse:
    headers = {"content-type": "application/json"}

    def __init__(self, content, status_code=200, finish_reason="stop", reasoning_content=None):
        self._content = content
        self.status_code = status_code
        self._finish_reason = finish_reason
        self._reasoning_content = reasoning_content

    def json(self):
        message = {"content": self._content}
        if self._reasoning_content is not None:
            message["reasoning_content"] = self._reasoning_content
        return {
            "choices": [{"message": message, "finish_reason": self._finish_reason}],
            "usage": {"completion_tokens": 7},
        }


def test_capture_predictions_uses_injected_http_client_without_recording_auth():
    calls = []
    prompt_pack = {
        "prompts": evals.build_prompt_pack()["prompts"][:2],
    }

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse("SELECT COUNT(*) AS ROW_COUNT FROM HR.EMPLOYEES")

    result = evals.capture_predictions(
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        api_key="secret-token-not-for-report",
        http_post=fake_post,
    )

    payload = json.dumps(result, ensure_ascii=False)
    assert result["model_call_attempted"] is True
    assert result["total"] == 2
    assert result["captured"] == 2
    assert set(result["outputs"]) == {item["id"] for item in prompt_pack["prompts"]}
    assert len(calls) == 2
    assert all(call["url"] == "http://localhost:1234/v1/chat/completions" for call in calls)
    assert all(call["headers"]["Authorization"] == "Bearer secret-token-not-for-report" for call in calls)
    assert "secret-token-not-for-report" not in payload
    assert "Authorization" not in payload
    assert "http://localhost:1234" not in payload


def test_capture_predictions_does_not_send_runtime_json_mode_by_default():
    calls = []
    json_prompt = next(
        item for item in evals.build_prompt_pack()["prompts"] if item["response_format"] == "json_object"
    )
    prompt_pack = {"prompts": [json_prompt]}

    def fake_post(url, *, headers, json, timeout):
        calls.append(json)
        return _FakeResponse('{"operation":"CREATE"}')

    evals.capture_predictions(
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        http_post=fake_post,
    )

    assert "response_format" not in calls[0]


def test_capture_predictions_can_send_json_response_format_when_enabled():
    calls = []
    json_prompt = next(
        item for item in evals.build_prompt_pack()["prompts"] if item["response_format"] == "json_object"
    )
    prompt_pack = {"prompts": [json_prompt]}

    def fake_post(url, *, headers, json, timeout):
        calls.append(json)
        return _FakeResponse('{"operation":"CREATE"}')

    evals.capture_predictions(
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        http_post=fake_post,
        use_runtime_json_mode=True,
    )

    assert calls[0]["response_format"] == {"type": "json_object"}


def test_capture_predictions_does_not_store_http_error_body_as_output():
    prompt_pack = {
        "prompts": [evals.build_prompt_pack()["prompts"][0]],
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse("server error body should not be stored", status_code=400)

    result = evals.capture_predictions(
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        http_post=fake_post,
    )

    assert result["captured"] == 0
    assert result["failed"] == 1
    assert result["outputs"] == {}
    assert result["results"][0]["status"] == "http_error"
    assert "server error body should not be stored" not in json.dumps(result, ensure_ascii=False)


def test_capture_predictions_treats_empty_content_as_failed_not_captured():
    prompt_pack = {
        "prompts": [evals.build_prompt_pack()["prompts"][0]],
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse("", finish_reason="length", reasoning_content="private reasoning text")

    result = evals.capture_predictions(
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        http_post=fake_post,
    )
    payload = json.dumps(result, ensure_ascii=False)

    assert result["captured"] == 0
    assert result["failed"] == 1
    assert result["empty"] == 1
    assert result["outputs"] == {}
    assert result["results"][0]["status"] == "empty_output"
    assert result["results"][0]["finish_reason"] == "length"
    assert result["results"][0]["has_reasoning_content"] is True
    assert "private reasoning text" not in payload


@pytest.mark.parametrize("body", [None, [], {}, {"choices": []}, {"choices": None}, {"choices": [None]}])
def test_chat_extractors_handle_malformed_json_shapes_without_widening_errors(body):
    class Response:
        headers = {"content-type": "application/json"}

        def json(self):
            return body

    assert evals._extract_chat_content(Response()) == ""
    assert evals._extract_chat_metadata(Response()) == {"finish_reason": None, "has_reasoning_content": False}


@pytest.mark.parametrize("name", ["_extract_chat_content", "_extract_chat_metadata"])
def test_chat_extractors_handle_invalid_json_but_not_unexpected_callback_failures(name):
    class Response:
        headers = {"content-type": "application/json"}

        def json(self):
            raise ValueError("synthetic invalid JSON")

    fallback = "" if name == "_extract_chat_content" else {"finish_reason": None, "has_reasoning_content": False}
    assert getattr(evals, name)(Response()) == fallback

    class Broken(Response):
        def json(self):
            raise RuntimeError("synthetic private callback diagnostic")

    with pytest.raises(RuntimeError):
        getattr(evals, name)(Broken())


@pytest.mark.parametrize("failure_on", [1, 2])
def test_unexpected_json_callback_failure_is_redacted_by_capture_boundary(failure_on):
    class Response:
        headers = {"content-type": "application/json"}
        status_code = 200
        calls = 0

        def json(self):
            self.calls += 1
            if self.calls == failure_on:
                raise RuntimeError("synthetic private callback diagnostic")
            return {"choices": [{"message": {"content": "synthetic safe output"}}]}

    response = Response()
    pack = {"prompts": evals.build_prompt_pack()["prompts"][:1]}
    report = evals.capture_predictions(prompt_pack=pack, base_url="http://localhost:1234/v1",
        model="synthetic-model", http_post=lambda *args, **kwargs: response)
    assert report["captured"] == 0 and report["failed"] == 1 and report["outputs"] == {}
    assert report["results"][0]["status"] == "http_error"
    assert report["results"][0]["error_type"] == "RuntimeError"
    assert "synthetic private" not in json.dumps(report)


@pytest.mark.parametrize("content", [None, 0, False, [], {}, {"text": "synthetic"}])
def test_capture_null_or_nontext_content_is_not_a_model_answer(content):
    class Response:
        headers = {"content-type": "application/json"}
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}

    report = evals.capture_predictions(prompt_pack={"prompts": evals.build_prompt_pack()["prompts"][:1]},
        base_url="http://localhost:1234/v1", model="synthetic-model", http_post=lambda *args, **kwargs: Response())
    assert report["captured"] == 0 and report["outputs"] == {}
    assert report["failed"] == 1 and report["empty"] == 1
    assert report["results"][0]["status"] == "empty_output"


def test_capture_predictions_blocks_sensitive_model_output():
    prompt_pack = {
        "prompts": [evals.build_prompt_pack()["prompts"][0]],
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse("PASSWORD=not-for-output")

    result = evals.capture_predictions(
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        http_post=fake_post,
    )

    case_id = prompt_pack["prompts"][0]["id"]
    assert result["captured"] == 0
    assert result["outputs"] == {}
    assert result["results"][0]["id"] == case_id
    assert result["results"][0]["status"] == "blocked_output"
    assert result["results"][0]["blocked_findings"]
    assert "PASSWORD=not-for-output" not in json.dumps(result, ensure_ascii=False)


def test_export_predictions_writes_predictions_file_without_prompt_or_auth(tmp_path):
    path = tmp_path / "predictions.json"
    prompt_pack = {
        "prompts": [evals.build_prompt_pack()["prompts"][0]],
    }

    def fake_post(*args, **kwargs):
        return _FakeResponse("SELECT COUNT(*) AS ROW_COUNT FROM HR.EMPLOYEES")

    result = evals.export_predictions(
        path,
        prompt_pack=prompt_pack,
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        api_key="secret-token-not-for-report",
        http_post=fake_post,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(saved, ensure_ascii=False)

    assert result["status"] == "written"
    assert saved["model"] == "local-test-model"
    assert saved["captured"] == 1
    assert saved["outputs"]
    assert "prompt" not in payload.lower()
    assert "secret-token-not-for-report" not in payload
    assert "http://localhost:1234" not in payload


class _FakeModelsResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, models):
        self._models = models

    def json(self):
        return {"data": [{"id": model} for model in self._models]}


def test_probe_runtime_checks_model_list_without_recording_base_url_or_auth():
    calls = []

    def fake_get(url, *, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _FakeModelsResponse(["local-test-model", "other-model"])

    result = evals.probe_runtime(
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        api_key="secret-token-not-for-report",
        http_get=fake_get,
    )
    payload = json.dumps(result, ensure_ascii=False)

    assert result["mode"] == "runtime_probe"
    assert result["model_call_attempted"] is False
    assert result["live_execution_attempted"] is False
    assert result["reachable"] is True
    assert result["model_available"] is True
    assert result["model_count"] == 2
    assert calls == [
        {
            "url": "http://localhost:1234/v1/models",
            "headers": {"Authorization": "Bearer secret-token-not-for-report"},
            "timeout": 10,
        }
    ]
    assert "secret-token-not-for-report" not in payload
    assert "Authorization" not in payload
    assert "http://localhost:1234" not in payload


def test_probe_runtime_records_only_error_type_when_endpoint_fails():
    def fake_get(*args, **kwargs):
        raise RuntimeError("http://user:password@local.example/v1 failed")

    result = evals.probe_runtime(
        base_url="http://localhost:1234/v1",
        model="local-test-model",
        http_get=fake_get,
    )
    payload = json.dumps(result, ensure_ascii=False)

    assert result["reachable"] is False
    assert result["model_available"] is False
    assert result["error_type"] == "RuntimeError"
    assert "password" not in payload
    assert "local.example" not in payload


def test_cli_runtime_probe_writes_report_without_static_eval_or_model_call(tmp_path, monkeypatch):
    path = tmp_path / "runtime_probe.json"

    def fake_get(url, *, headers, timeout):
        return _FakeModelsResponse(["local-test-model"])

    monkeypatch.setattr(evals.requests, "get", fake_get)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_model_eval.py",
            "--probe-runtime",
            "--runtime-probe-out",
            str(path),
            "--base-url",
            "http://localhost:1234/v1",
            "--model",
            "local-test-model",
            "--api-key",
            "secret-token-not-for-report",
        ],
    )

    exit_code = evals.main()
    saved = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(saved, ensure_ascii=False)

    assert exit_code == 0
    assert saved["mode"] == "runtime_probe"
    assert saved["reachable"] is True
    assert saved["model_available"] is True
    assert saved["model_call_attempted"] is False
    assert saved["live_execution_attempted"] is False
    assert "secret-token-not-for-report" not in payload
    assert "http://localhost:1234" not in payload


@pytest.mark.parametrize(
    "text",
    [
        "TOKEN=abc123",
        "jdbc:oracle:thin:@host:1521/service",
        "user/password@dbhost:1521/service",
        "sample_rows=[{'ID': 1}]",
    ],
)
def test_blocked_content_scanner_rejects_secret_connection_and_row_patterns(text):
    assert evals.scan_blocked_content(text)
