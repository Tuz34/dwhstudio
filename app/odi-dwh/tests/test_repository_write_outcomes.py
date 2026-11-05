"""Actual parser boundaries, using synthetic metadata only."""
import json

import pytest

import odi_orchestrator
import scd2_odi
import scd2_builder
from dim_product_odi_scd2_scenario import normalize_dim_product_scd2_scenario_generation
from tests.test_dim_product_odi_scd2_scenario_package import _raw_generation_success


def failure(script="scd2_mapping_create.groovy", outcome="commit_outcome_unknown"):
    tool, mode = scd2_odi._TOOLS[script]
    clean = outcome in {"no_transaction", "rolled_back"}
    committed = outcome == "committed"
    return dict(tool=tool, mode=mode, status="error" if clean else "committed_report_incomplete" if committed else "outcome_unknown",
                side_effects=False if clean else True if committed else None,
                repository_connection_attempted=True, raw_rows_included=False, pk_values_included=False,
                secret_printed=False, error_type="SyntheticError", repository_outcome=outcome,
                commit_attempted=outcome in {"commit_outcome_unknown", "committed"}, commit_acknowledged=committed,
                rollback_attempted=outcome not in {"no_transaction", "committed"},
                rollback_succeeded=outcome in {"rolled_back", "commit_outcome_unknown"},
                manual_recovery_review_required=not clean)


@pytest.mark.parametrize("script", [scd2_odi.SCRIPT_MAPPING, scd2_odi.SCRIPT_REGISTER])
@pytest.mark.parametrize("outcome", ["no_transaction", "rolled_back", "rollback_failed", "commit_outcome_unknown", "committed"])
def test_generic_writer_failure_retains_exact_repository_state(script, outcome):
    raw = failure(script, outcome)
    _, result = scd2_odi._parse_envelope(script, "write", json.dumps(raw))
    assert result["status"] == "failed"
    for key in ("repository_outcome", "side_effects", "commit_attempted", "commit_acknowledged",
                "rollback_attempted", "rollback_succeeded", "manual_recovery_review_required"):
        assert result[key] == raw[key]
    assert odi_orchestrator._strict_structured_error_output(script, json.dumps(raw)) is True


@pytest.mark.parametrize("script", [scd2_odi.SCRIPT_MAPPING, scd2_odi.SCRIPT_REGISTER])
@pytest.mark.parametrize("output", ["not json", "Hata: error_type=RemoteTimeout", '{"status":"passed","status":"error"}'])
def test_unusable_write_output_cannot_prove_no_effects(script, output):
    _, result = scd2_odi._parse_envelope(script, "write", output)
    assert result["side_effects"] is None and result["repository_outcome"] == "unverified"
    assert result["manual_recovery_review_required"] is True


@pytest.mark.parametrize("mutate", [
    {"side_effects": False}, {"commit_attempted": False}, {"rollback_succeeded": 1},
    {"manual_recovery_review_required": False}, {"unexpected": "SYNTHETIC_PRIVATE"},
])
def test_malformed_failure_is_unverified_and_never_echoed(mutate):
    raw = {**failure(), **mutate}
    _, result = scd2_odi._parse_envelope(scd2_odi.SCRIPT_MAPPING, "write", json.dumps(raw))
    assert result["repository_outcome"] == "unverified" and result["side_effects"] is None
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert not odi_orchestrator._strict_structured_error_output(scd2_odi.SCRIPT_MAPPING, json.dumps(raw))


def test_fixed_generator_accepts_real_generator_latest_metadata_without_echoing_it():
    raw = _raw_generation_success()
    raw["scenario"]["latest_version"] = "001"
    result = normalize_dim_product_scd2_scenario_generation(raw)
    assert result["status"] == "passed" and set(result["scenario"]) == {"name", "version"}


def test_fixed_generator_unusable_output_is_not_a_no_effects_claim():
    result = normalize_dim_product_scd2_scenario_generation({"status": "error"})
    assert result["side_effects"] is None and result["manual_recovery_review_required"] is True


@pytest.mark.parametrize("outcome", ["no_transaction", "rolled_back", "rollback_failed", "commit_outcome_unknown", "committed"])
def test_fixed_generator_preserves_public_generator_failure_contract(outcome):
    raw = failure(outcome=outcome)
    raw.update(tool="odi.generate_scenario_metadata", mode="scenario_metadata_generation")
    for key in ("raw_rows_included", "pk_values_included", "secret_printed"):
        raw.pop(key)
    raw["checks"] = [{"name": "scenario_generation_exception", "status": "fail"}]
    result = normalize_dim_product_scd2_scenario_generation(raw)
    assert result["side_effects"] is raw["side_effects"]
    assert result["repository_outcome"] == outcome
    assert odi_orchestrator._strict_structured_error_output("generate_scenario_metadata.groovy", json.dumps(raw))


@pytest.mark.parametrize("outcome", ["no_transaction", "rolled_back", "rollback_failed", "commit_outcome_unknown", "committed"])
def test_fixed_registrar_preserves_transaction_state(outcome):
    from dim_product_odi_scd2_datastore_registration import normalize_dim_product_odi_scd2_datastore_registration
    raw = failure(outcome=outcome)
    raw.update(tool="odi.dim_product_scd2_datastore_registration", mode="fixed_absent_only_odi_datastore_registration")
    result = normalize_dim_product_odi_scd2_datastore_registration(raw)
    assert result["repository_outcome"] == outcome and result["side_effects"] is raw["side_effects"]
    assert odi_orchestrator._strict_structured_error_output("dim_product_scd2_datastore_register.groovy", json.dumps(raw))


def test_fixed_importer_rejected_report_does_not_prove_no_effects():
    from dim_product_odi_scd2_ikm_import import normalize_dim_product_odi_scd2_ikm_import
    result = normalize_dim_product_odi_scd2_ikm_import({"status": "error"})
    assert result["side_effects"] is None and result["manual_recovery_review_required"] is True


@pytest.mark.parametrize("field", ["primary_key_column_count", "natural_key_column_count", "overwrite_attributes"])
def test_registered_datastore_requires_actual_planned_key_and_role_counts(field):
    from tests.scd2_support import REQUEST, SOURCE_COLUMNS, FakeOrchestrator, register_payload
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    raw = json.loads(register_payload(plan))
    section = raw["result"]["scd_role_column_counts"] if field == "overwrite_attributes" else raw["result"]
    section[field] = 0
    orchestrator = FakeOrchestrator(plan, responses={scd2_odi.SCRIPT_REGISTER: json.dumps(raw)})
    result = scd2_odi.register_datastore(orchestrator, plan, True)
    assert result["status"] == "failed" and result["side_effects"] is True


@pytest.mark.parametrize("script", [scd2_odi.SCRIPT_MAPPING, scd2_odi.SCRIPT_REGISTER, "dim_product_scd2_mapping_create.groovy", "generate_scenario_metadata.groovy"])
def test_real_transport_preserves_reviewed_failure_envelope(monkeypatch, script):
    from tests.test_scd2_groovy_scripts import _prepare_runtime, _Completed
    raw = failure(outcome="rolled_back")
    registered = odi_orchestrator._STRICT_STRUCTURED_ERROR_CONTRACTS[script]
    raw.update(tool=registered["tool"], mode=registered["mode"])
    if script == "generate_scenario_metadata.groovy":
        for key in ("raw_rows_included", "pk_values_included", "secret_printed"):
            raw.pop(key)
        raw["checks"] = [{"name": "scenario_generation_exception", "status": "fail"}]
    output = json.dumps(raw)
    _prepare_runtime(monkeypatch, _Completed(stdout=output))
    with odi_orchestrator.groovy_approval_context(True):
        assert odi_orchestrator.OdiOrchestrator().run_groovy(script, {}) == output


@pytest.mark.parametrize("state", [failure(), failure(outcome="committed"), failure(outcome="rolled_back")])
def test_generation_surfaces_step_recovery_flags_at_top_level(state):
    import scd2_service
    report, _ = scd2_service._generate_response("partial_failure_mapping_create", None, [state], 502)
    expected_unknown = state["side_effects"] is None
    assert report["outcome_unknown"] is expected_unknown
    assert report["manual_review_required"] is (expected_unknown or state["side_effects"] is True)


def test_clean_generation_has_no_manual_flag_and_partial_ddl_has_one():
    import scd2_service
    clean, _ = scd2_service._generate_response("generated", None, [], 200)
    assert clean["manual_review_required"] is False and clean["outcome_unknown"] is False
    failed, _ = scd2_service._generate_response("partial_failure_target_ddl", None, [{"sequence_created": True}], 502)
    assert failed["manual_review_required"] is True and failed["known_side_effects"] is True
