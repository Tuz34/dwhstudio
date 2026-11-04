from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAME = "dim_product_scd2_datastore_register.groovy"
APPROVAL_SCOPE = "odi68_dim_product_odi_scd2_datastore_registration"


def _capability_probe(**overrides):
    report = {
        "tool": "odi.dim_product_scd2_capability_probe",
        "status": "passed",
        "mode": "normalized_odi_sdk_metadata_only",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "capability": {
            "status": "passed",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "target_technology": None,
            "target_ikm": None,
            "target_ikm_installed": False,
            "target_ikm_assignable": False,
            "lkm": "LKM SQL to Oracle (Built-In)",
            "scd2_role_assignment_supported": True,
        },
        "datastore": {
            "status": "passed",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "source": {"owner": "DWH", "table": "ODS_SH_PRODUCTS", "column_count": 24},
            "target": {"owner": "DWH", "table": "DIM_PRODUCT", "column_count": 0},
            "target_keys": {"primary_key_columns": [], "natural_key_columns": []},
            "scd_roles": {
                "surrogate_key": [],
                "natural_key": [],
                "start_date": [],
                "end_date": [],
                "current_flag": [],
                "tracked_attributes": [],
            },
        },
        "objects": {
            "status": "attention_required",
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "mapping": {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT", "exists": False},
            "scenario": {
                "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN",
                "version": "001",
                "exists": False,
            },
        },
    }
    for key, value in overrides.items():
        if "." not in key:
            report[key] = value
            continue
        parent, child = key.split(".", 1)
        report[parent][child] = value
    return report


def _plan(capability_probe=None):
    from dim_product_odi_scd2_datastore_registration import (
        build_dim_product_odi_scd2_datastore_registration_plan,
    )

    return build_dim_product_odi_scd2_datastore_registration_plan(
        capability_probe or _capability_probe()
    )


def _request(**overrides):
    from dim_product_odi_scd2_datastore_registration import (
        build_dim_product_odi_scd2_datastore_registration_request,
    )

    values = {
        "plan": _plan(),
        "capability_probe_review_status": "approved",
        "registration_contract_review_status": "approved",
        "registrar_implementation_review_status": "approved",
        "approved": True,
        "approval_scope": APPROVAL_SCOPE,
    }
    values.update(overrides)
    return build_dim_product_odi_scd2_datastore_registration_request(**values)


def _raw_success_payload():
    return {
        "tool": "odi.dim_product_scd2_datastore_registration",
        "status": "passed",
        "mode": "fixed_absent_only_odi_datastore_registration",
        "side_effects": True,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "result": {
            "status": "passed",
            "model_code": "TGT_DWH",
            "target_owner": "DWH",
            "target_table": "DIM_PRODUCT",
            "target_column_count": 26,
            "primary_key_columns": ["PRODUCT_SK"],
            "natural_key_columns": ["PROD_ID"],
            "scd_role_column_counts": {
                "surrogate_key": 1,
                "natural_key": 1,
                "start_date": 1,
                "end_date": 1,
                "current_flag": 1,
                "tracked_attributes": 17,
            },
            "commit_completed": True,
        },
    }


def test_static_registration_plan_freezes_exact_absent_only_scd2_metadata():
    plan = _plan()

    assert plan["status"] == "ready_for_registration_implementation_review"
    assert plan["model_code"] == "TGT_DWH"
    assert plan["target"] == {"owner": "DWH", "table": "DIM_PRODUCT"}
    assert plan["approval_scope"] == APPROVAL_SCOPE
    assert plan["absent_only"] is True
    assert plan["replace_existing_datastore"] is False
    assert plan["target_column_count"] == 26
    assert plan["primary_key"] == {
        "name": "PK_DIM_PRODUCT",
        "columns": ["PRODUCT_SK"],
        "in_database": True,
    }
    assert plan["natural_key"] == {
        "name": "AK_DIM_PRODUCT_PROD_ID",
        "columns": ["PROD_ID"],
        "in_database": False,
    }
    assert plan["scd_roles"]["tracked_attributes"] and len(
        plan["scd_roles"]["tracked_attributes"]
    ) == 17
    assert plan["will_execute"] is False
    assert plan["connection_attempted"] is False


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (_capability_probe(status="error"), "blocked_capability_probe"),
        (
            _capability_probe(**{"datastore.target": {"owner": "DWH", "table": "DIM_PRODUCT", "column_count": 26}}),
            "blocked_target_datastore_not_absent",
        ),
        (
            _capability_probe(**{"objects.mapping": {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT", "exists": True}}),
            "blocked_target_object_collision",
        ),
    ],
)
def test_registration_plan_fails_closed_on_stale_or_colliding_capability(probe, expected):
    assert _plan(probe)["status"] == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"plan": {"status": "blocked"}}, "blocked_registration_plan"),
        ({"capability_probe_review_status": "pending"}, "blocked_capability_probe_review"),
        ({"registration_contract_review_status": "pending"}, "blocked_registration_contract_review"),
        ({"registrar_implementation_review_status": "pending"}, "blocked_registrar_implementation_review"),
        ({"approved": "yes"}, "blocked_until_explicit_approval"),
        ({"approved": 1}, "blocked_until_explicit_approval"),
        ({"approval_scope": "wrong_scope"}, "blocked_until_exact_approval_scope"),
    ],
)
def test_registration_request_fails_closed_before_future_repository_write(overrides, expected):
    result = _request(**overrides)

    assert result["status"] == expected
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False
    assert result["write_operations_attempted"] == []


def test_all_request_gates_only_make_dedicated_registrar_ready():
    result = _request()

    assert result["status"] == "ready_for_dedicated_registrar_execution"
    assert result["will_execute"] is False
    assert result["connection_attempted"] is False


def test_registration_output_normalizer_accepts_only_exact_count_status_metadata():
    from dim_product_odi_scd2_datastore_registration import (
        normalize_dim_product_odi_scd2_datastore_registration,
    )

    result = normalize_dim_product_odi_scd2_datastore_registration(
        _raw_success_payload()
    )

    assert result["status"] == "passed"
    assert result["result"]["target_column_count"] == 26
    assert result["result"]["commit_completed"] is True
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.update({"unexpected": "repository-id"}),
        lambda p: p["result"].update({"sample_key": "not-allowed"}),
        lambda p: p["result"].update({"target_column_count": True}),
        lambda p: p["result"].update({"commit_completed": 1}),
    ],
)
def test_registration_output_normalizer_rejects_extra_or_weakly_typed_fields(mutator):
    from dim_product_odi_scd2_datastore_registration import (
        normalize_dim_product_odi_scd2_datastore_registration,
    )

    payload = deepcopy(_raw_success_payload())
    mutator(payload)
    result = normalize_dim_product_odi_scd2_datastore_registration(payload)

    assert result["status"] == "blocked_invalid_registration_output"
    assert "repository-id" not in json.dumps(result)
    assert "not-allowed" not in json.dumps(result)


def test_orchestrator_registration_wrapper_never_runs_before_every_gate(monkeypatch):
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "run_groovy",
        lambda script, params: calls.append((script, params)) or "should not run",
    )

    result = orchestrator.dim_product_scd2_datastore_register(
        capability_probe=_capability_probe(),
        capability_probe_review_status="approved",
        registration_contract_review_status="approved",
        registrar_implementation_review_status="pending",
        approved=True,
        approval_scope=APPROVAL_SCOPE,
    )

    assert result["status"] == "blocked_registrar_implementation_review"
    assert calls == []


def test_orchestrator_registration_wrapper_uses_fixed_script_and_no_object_params(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    calls = []

    def fake_run(script, params):
        calls.append((script, params, odi_orchestrator._GROOVY_APPROVED.get()))
        return json.dumps(_raw_success_payload())

    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)

    result = orchestrator.dim_product_scd2_datastore_register(
        capability_probe=_capability_probe(),
        capability_probe_review_status="approved",
        registration_contract_review_status="approved",
        registrar_implementation_review_status="approved",
        approved=True,
        approval_scope=APPROVAL_SCOPE,
    )

    assert result["status"] == "passed"
    assert calls == [(SCRIPT_NAME, {}, True)]
    assert odi_orchestrator._GROOVY_APPROVED.get() is False


def test_dedicated_groovy_registrar_is_fixed_absent_only_and_non_destructive():
    import odi_orchestrator

    script = (PROJECT_ROOT / "odi_automation" / SCRIPT_NAME).read_text(
        encoding="utf-8"
    )
    lower = script.lower()

    assert odi_orchestrator.GROOVY_ACTION_REGISTRY[SCRIPT_NAME].category == "repository_write"
    for required in (
        'final String MODEL_CODE = "TGT_DWH"',
        'final String TARGET_TABLE = "DIM_PRODUCT"',
        "PK_DIM_PRODUCT",
        "AK_DIM_PRODUCT_PROD_ID",
        "SURROGATE_KEY",
        "NATURAL_KEY",
        "ADD_ROW_ON_CHANGE",
        "CURRENT_RECORD_FLAG",
        "START_TIMESTAMP",
        "END_TIMESTAMP",
        "setInDatabase(false)",
        "target datastore already exists",
    ):
        assert required in script

    for forbidden in (
        "system.getproperty(\"odi.model",
        "system.getproperty(\"odi.table",
        ".remove(",
        "drop ",
        "delete ",
        "truncate ",
        "printstacktrace",
        "error_detail",
        "getinternalid",
        "getexpression",
    ):
        assert forbidden not in lower


def test_registration_runbook_keeps_live_write_and_scenario_actions_separate():
    runbook = (
        PROJECT_ROOT
        / "docs"
        / "dwh-dim-product-odi-scd2-datastore-registration.md"
    ).read_text(encoding="utf-8")
    lower = runbook.lower()

    for required in (
        "absent-only",
        "separate independent review",
        APPROVAL_SCOPE,
        "does not authorize mapping creation",
        "does not authorize scenario generation",
        "does not authorize scenario run",
        "no raw rows",
        "no pk values",
    ):
        assert required.lower() in lower
