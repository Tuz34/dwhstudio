"""Screen-edge rulings for skipped steps, mapping layers, and preflight text."""

import json

import pytest

from app_helpers import _group_odi_mapping_lineage_items
from mapping_generator import MappingGenerator
from routes.error_responses import redacted_operation_result
from skip_sentences import EXPECTED_SKIP_SENTENCES, STEP_SKIPPED


_AUDIT_SKIP = "Audit preservation does not apply to this load."
_SKEY_SKIP = "SKEY not requested"
_SOURCE_DATASTORE_SKIP = "Source datastore already exists."
_TARGET_DATASTORE_SKIP = "Target datastore already exists."
_SKEY_KEY_SKIP = "skey_natural_key was not specified"
_NO_SKEY_CHANGE = "No SKEY change"
_TARGET_UNCHANGED = "Target table did not change"
_NO_ROLLBACK = "No rollback record found."

_ACCEPTANCE = (
    ("MAP_ODI81N_F1", "ODI81N_SRC_F1", "ODS_ODI81N_F1", "ODS"),
    ("MAP_ODI81N_F1_UI", "ODI81N_SRC_F1", "ODS_ODI81N_F1_UI", "ODS"),
    ("MAP_ODI81N_F1_V2", "ODI81N_SRC_F1", "ODS_ODI81N_F1_V2", "ODS"),
    ("MAP_ODI81N_F2_UI", "ODI81N_SRC_F2", "ODS_ODI81N_F2_UI", "ODS"),
    ("MAP_ODI81N_F3_UI", "ODI81N_SRC_F3", "EDW_ODI81N_F3_UI", "EDW"),
    ("MAP_ODI81N_F3_V2", "ODI81N_SRC_F3", "EDW_ODI81N_F3_V2", "EDW"),
    ("MAP_ODI81N_F4_UI", "ODS_ODI81N_F4", "DIM_ODI81N_F4_UI", "EDW"),
    ("MAP_ODI81N_F4_SCR", "ODS_ODI81N_F4", "DIM_ODI81N_F4_SCR", "EDW"),
    ("MAP_ODI81N_F5_UI", "DWH_ODI81N_F5", "DM_DIM_ODI81N_F5", "DM"),
    ("MAP_ODI81N_F5_SCR", "DWH_ODI81N_F5", "DM_DIM_ODI81N_F5_SCR", "DM"),
    ("MAP_ODI81N_F5B_UI", "DWH_ODI81N_F5", "DM_DIM_ODI81N_F5B", "DM"),
)


def _component(mapping, name, qualified, **extra):
    row = {
        "MAPPING_NAME": mapping,
        "COMP_NAME": name,
        "COMP_TYPE": "DATASTORE",
        "TABLE_QUALIFIED_NAME": f"DWH.{qualified}",
        "PROJECT_CODE": "ORA_DWH",
        "PROJECT_NAME": "Oracle DWH",
        "FOLDER_NAME": "FLD_CORE_MAPPINGS",
    }
    row.update(extra)
    return row


def _truncate_generator():
    """A truncate load logs audit preservation as an expected skip. No database."""
    generator = MappingGenerator()
    params = {
        "integration_type": "CONTROL_APPEND",
        "src_owner": "DWH",
        "tgt_owner": "DWH",
        "src_model": "ORACLE_DWH",
        "tgt_model": "ORACLE_ODS",
        "tgt_table": "ODS_ODI81N_F2_UI",
        "src_table": "ODI81N_SRC_F2",
        "update_key_columns": [],
    }
    plan = {
        "src_columns": [{"COLUMN_NAME": "ENTITY_ID"}],
        "desired_col_defs": [("ENTITY_ID", "NUMBER(10)")],
    }
    profile = generator._prepare_audit_preservation(params, plan)
    assert profile["qualified"] is False
    generator._add_log("create_skey", "SKIPPED", _SKEY_SKIP)
    generator._add_log("register_datastores", "SKIPPED", _SOURCE_DATASTORE_SKIP)
    generator._add_log("populate_lineage", "SKIPPED", "ORA-00942: secret table missing")
    return generator


def _classic_success(generator):
    return {
        "batch_id": generator._batch_id,
        "overall_status": "SUCCESS",
        "params": {"integration_type": "CONTROL_APPEND", "tgt_table": "ODS_ODI81N_F2_UI"},
        "steps": {
            "create_skey": {"skipped": True},
            "populate_lineage": {"skipped": True, "reason": "ORA-00942: secret table missing"},
        },
        "log": list(generator._log_entries),
    }


def test_expected_skip_sentences_are_the_closed_generator_set():
    assert EXPECTED_SKIP_SENTENCES == {
        _AUDIT_SKIP,
        _SKEY_SKIP,
        _SOURCE_DATASTORE_SKIP,
        _TARGET_DATASTORE_SKIP,
        _SKEY_KEY_SKIP,
        _NO_SKEY_CHANGE,
        _TARGET_UNCHANGED,
        _NO_ROLLBACK,
    }
    assert STEP_SKIPPED == "Step skipped."
    assert all("failed" not in sentence.lower() for sentence in EXPECTED_SKIP_SENTENCES)


def test_redactor_keeps_expected_skips_and_neutralizes_exception_skips():
    generator = _truncate_generator()
    audit = next(entry for entry in generator._log_entries if entry["step"] == "audit_preservation")
    assert audit["status"] == "SKIPPED"
    assert audit["detail"] == _AUDIT_SKIP

    cleaned = redacted_operation_result(_classic_success(generator), "mapping generation")
    details = {entry["step"]: entry["detail"] for entry in cleaned["log"]}
    assert details["audit_preservation"] == _AUDIT_SKIP
    assert details["create_skey"] == _SKEY_SKIP
    assert details["register_datastores"] == _SOURCE_DATASTORE_SKIP
    assert details["populate_lineage"] == STEP_SKIPPED
    assert cleaned["steps"]["populate_lineage"]["reason"] == STEP_SKIPPED
    rendered = json.dumps(cleaned)
    assert "secret table" not in rendered
    assert "mapping generation failed." not in rendered
    assert cleaned["overall_status"] == "SUCCESS"


def test_redactor_still_replaces_a_genuine_failure():
    secret = "ORA-00942: secret table missing"
    cleaned = redacted_operation_result(
        {
            "overall_status": "FAILED",
            "error": secret,
            "error_code": "target_changed_during_generation",
            "steps": {"create_mapping": {"status": "FAILED", "detail": secret}},
            "log": [
                {"step": "pipeline", "status": "FAILED", "detail": secret},
                {"step": "create_skey", "status": "SKIPPED", "detail": _SKEY_SKIP},
            ],
        },
        "mapping generation",
    )
    assert cleaned["error"] == "mapping generation failed."
    assert cleaned["error_type"] == "OperationError"
    assert cleaned["steps"]["create_mapping"]["detail"] == "mapping generation failed."
    assert cleaned["log"][0]["detail"] == "mapping generation failed."
    assert cleaned["log"][1]["detail"] == _SKEY_SKIP
    assert secret not in json.dumps(cleaned)
    assert cleaned["error_code"] == "target_changed_during_generation"


@pytest.mark.parametrize("mapping, source, target, layer", _ACCEPTANCE)
def test_acceptance_mapping_layer_comes_from_the_target_name(mapping, source, target, layer):
    grouped = {
        item["MAPPING_NAME"]: item
        for item in _group_odi_mapping_lineage_items([
            _component(mapping, source, source),
            _component(mapping, target, target),
        ])
    }
    found = grouped[mapping]
    assert found["TGT_TABLE"] == target
    assert found["TGT_OWNER"] == "DWH"
    assert found["LAYER"] == layer
    assert source in found["SRC_TABLE"]
    assert target not in found["SRC_TABLE"]


def test_mapping_without_a_recognisable_target_has_an_empty_layer():
    [found] = _group_odi_mapping_lineage_items([
        _component("MAP_PLAIN", "EMPLOYEES", "EMPLOYEES"),
        {
            "MAPPING_NAME": "MAP_PLAIN",
            "COMP_NAME": "CUSTOMERS",
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": "DWH.CUSTOMERS",
        },
    ])
    assert found["LAYER"] == ""
    assert found["LAYER"] != "STG"
    assert found["TGT_TABLE"] == "?"


def test_structural_target_wins_over_a_source_name_prefix():
    [found] = _group_odi_mapping_lineage_items([
        _component("MAP_STRUCT", "ODS_SOURCE", "ODS_SOURCE"),
        _component("MAP_STRUCT", "CUSTOMERS", "CUSTOMERS", EXPRESSION="ODS_SOURCE.ENTITY_ID"),
    ])
    assert found["TGT_TABLE"] == "CUSTOMERS"
    assert found["LAYER"] == ""
    assert "ODS_SOURCE" in found["SRC_TABLE"]


def test_mappings_route_uses_the_same_layer_rules(app_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "odi_get_mapping_lineage",
        lambda limit=100: json.dumps([
            _component("MAP_ODI81N_F3_UI", "ODI81N_SRC_F3", "ODI81N_SRC_F3"),
            _component("MAP_ODI81N_F3_UI", "EDW_ODI81N_F3_UI", "EDW_ODI81N_F3_UI"),
        ]),
    )
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    [mapping] = response.get_json()
    assert mapping["TGT_TABLE"] == "EDW_ODI81N_F3_UI"
    assert mapping["LAYER"] == "EDW"
    assert mapping["folder_name"] == "FLD_CORE_MAPPINGS"
    assert mapping["project_code"] == "ORA_DWH"


def test_external_preflight_repository_failure_keeps_class_name_only(monkeypatch):
    from tests.test_e2e_repository_preflight import _f1_generator, _quiet_database

    _quiet_database(monkeypatch)
    generator, params = _f1_generator()
    params = dict(params, src_model="ORACLE_SOURCE")

    def explode(_params):
        raise RuntimeError("password=secret\nhost=10.1.2.3")

    monkeypatch.setattr(generator, "_resolve_odi_repository_bindings", explode)
    failed = generator.target_table_preflight(params)
    repository = failed["repository"]
    assert repository["status"] == "unavailable"
    assert repository["error"] == "Repository identity check failed."
    assert repository["error_type"] == "RuntimeError"
    rendered = json.dumps(failed)
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "10.1.2.3" not in rendered
    assert "RuntimeError" in rendered
