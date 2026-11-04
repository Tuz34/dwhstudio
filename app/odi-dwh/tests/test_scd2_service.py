from __future__ import annotations

import pytest

import scd2_builder
import scd2_service
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    FakeOrchestrator,
    error_payload,
    make_services,
    oracle_error,
    plan_catalog_rows,
    probe_payload,
)


@pytest.fixture
def plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


def ddl_handler(plan, **kw):
    rows = plan_catalog_rows(plan)
    fail_on = kw.get("fail_on")

    def handler(sql, params):
        if fail_on and fail_on in sql:
            return oracle_error("ORA-01031")
        if "FROM ALL_TABLES" in sql:
            return [(kw.get("table_exists", 0),)]
        if "FROM ALL_SEQUENCES" in sql:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return rows
        return []

    return handler


def step_names(payload):
    return [s["step"] for s in payload["steps"]]


def test_plan_preview_from_request_columns_is_connectionless(plan):
    catalog_log = []
    services = make_services(FakeOrchestrator(plan), catalog_log=catalog_log)
    body = {**REQUEST, "source_columns": SOURCE_COLUMNS}
    payload, status = scd2_service.plan_preview(body, services)
    assert status == 200 and payload["status"] == "planned"
    assert payload["metadata_source"] == "request"
    assert catalog_log == []
    assert payload["plan"]["connection_attempted"] is False
    assert payload["ddl"]["create_table"].startswith('CREATE TABLE "DM"."DIM_CUSTOMER"')
    assert payload["odi_expressions"]["CUSTOMER_SK"] == "DM.SEQ_DIM_CUSTOMER.NEXTVAL"
    assert payload["write_operations_attempted"] == []


def test_plan_preview_reads_catalog_metadata_only_when_columns_missing(plan):
    catalog_log = []
    services = make_services(FakeOrchestrator(plan), catalog_log=catalog_log)
    payload, status = scd2_service.plan_preview(REQUEST, services)
    assert status == 200 and payload["metadata_source"] == "live_catalog"
    assert len(catalog_log) == 1 and "ALL_TAB_COLUMNS" in catalog_log[0]
    assert "SELECT COLUMN_NAME" in catalog_log[0] and "ODS_CUSTOMERS" not in catalog_log[0]


def test_plan_preview_error_statuses(plan):
    services = make_services(FakeOrchestrator(plan))
    payload, status = scd2_service.plan_preview({**REQUEST, "target_owner": "SYS"}, services)
    assert status == 400 and payload["error_code"] == "target_owner_not_allowed"
    payload, status = scd2_service.plan_preview({**REQUEST, "natural_key": ["NOPE"], "source_columns": SOURCE_COLUMNS}, services)
    assert status == 400 and payload["error_code"] == "natural_key_not_in_source"
    empty = make_services(FakeOrchestrator(plan), catalog_handler=lambda sql, params: [])
    payload, status = scd2_service.plan_preview(REQUEST, empty)
    assert status == 404 and payload["status"] == "blocked_source_not_found"
    broken = make_services(FakeOrchestrator(plan), catalog_handler=lambda sql, params: oracle_error("ORA-00942 detail"))
    payload, status = scd2_service.plan_preview(REQUEST, broken)
    assert status == 503 and "ORA-00942" not in str(payload)


def test_generate_happy_path_runs_steps_in_order(plan):
    orchestrator = FakeOrchestrator(plan)
    ddl_log = []
    services = make_services(orchestrator, ddl_log=ddl_log, ddl_handler=ddl_handler(plan))
    payload, status = scd2_service.generate(REQUEST, services, approved=True)
    assert status == 200 and payload["status"] == "generated"
    assert step_names(payload) == [
        "odi_probe",
        "target_ddl",
        "odi_datastore_register",
        "odi_mapping_create",
        "odi_scenario_generate",
    ]
    assert orchestrator.order == [
        "scd2_probe.groovy",
        "scd2_datastore_register.groovy",
        "scd2_mapping_create.groovy",
        "scenario_generate",
    ]
    assert any(sql.startswith("CREATE TABLE") for sql in ddl_log)
    assert payload["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN"


def test_generate_requires_literal_approval_and_does_nothing_otherwise(plan):
    for approved in (False, None, "true", 1):
        orchestrator = FakeOrchestrator(plan)
        ddl_log = []
        payload, status = scd2_service.generate(
            REQUEST, make_services(orchestrator, ddl_log=ddl_log), approved=approved
        )
        assert status == 400 and payload["status"] == "blocked_until_approval"
        assert orchestrator.calls == [] and ddl_log == []


def test_dm_model_missing_blocks_before_any_write_with_topology_steps(plan):
    orchestrator = FakeOrchestrator(
        plan, responses={"scd2_probe.groovy": probe_payload(target_model_exists=False)}
    )
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log, ddl_handler=ddl_handler(plan)), approved=True
    )
    assert status == 409 and payload["status"] == "blocked_dm_model_missing"
    assert payload["required_model_code"] == "TGT_DM"
    assert len(payload["required_topology_steps"]) == 3
    assert "Topology" in payload["required_topology_steps"][0]
    assert ddl_log == []  # no target DDL
    assert orchestrator.order == ["scd2_probe.groovy"]


def test_dwh_model_missing_is_a_generic_block(plan):
    request = {**REQUEST, "target_owner": "DWH"}
    dwh_plan = scd2_builder.build_scd2_plan(request, SOURCE_COLUMNS)
    orchestrator = FakeOrchestrator(
        dwh_plan, responses={"scd2_probe.groovy": probe_payload(target_model_exists=False)}
    )
    payload, status = scd2_service.generate(request, make_services(orchestrator), approved=True)
    assert payload["status"] == "blocked_target_model_missing" and status == 409


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"source_datastore_exists": False}, "blocked_source_datastore_missing"),
        ({"source_model_exists": False}, "blocked_source_model_missing"),
        ({"lkm_present": False}, "blocked_lkm_missing"),
        ({"project_folder_exists": False}, "blocked_project_folder_missing"),
        ({"target_model_oracle": False}, "blocked_target_model_not_oracle"),
        ({"mapping_exists": True, "target_datastore_exists": False}, "blocked_mapping_exists_unrelated"),
    ],
)
def test_other_probe_blocks_happen_before_ddl(plan, override, expected):
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(**override)})
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log), approved=True
    )
    assert payload["status"] == expected and status == 409
    assert ddl_log == []


def test_ddl_failure_stops_before_odi_writes(plan):
    orchestrator = FakeOrchestrator(plan)
    services = make_services(orchestrator, ddl_handler=ddl_handler(plan, fail_on="CREATE SEQUENCE"))
    payload, status = scd2_service.generate(REQUEST, services, approved=True)
    assert payload["status"] == "blocked_ddl_privilege" and status == 409
    assert orchestrator.order == ["scd2_probe.groovy"]


def test_ddl_partial_state_is_surfaced(plan):
    orchestrator = FakeOrchestrator(plan)
    services = make_services(orchestrator, ddl_handler=ddl_handler(plan, fail_on="CREATE TABLE"))
    payload, status = scd2_service.generate(REQUEST, services, approved=True)
    assert payload["status"] == "partial_failure_target_ddl" and status == 502
    ddl_step = payload["steps"][-1]
    assert ddl_step["sequence_created"] is True and ddl_step["target_created"] is False
    assert orchestrator.order == ["scd2_probe.groovy"]


def test_register_failure_reports_partial_state_with_completed_steps(plan):
    orchestrator = FakeOrchestrator(
        plan,
        responses={
            "scd2_datastore_register.groovy": error_payload(
                "odi.scd2_datastore_register",
                "generic_absent_only_scd2_datastore_registration",
                "TargetModelUnavailable",
            )
        },
    )
    services = make_services(orchestrator, ddl_handler=ddl_handler(plan))
    payload, status = scd2_service.generate(REQUEST, services, approved=True)
    assert payload["status"] == "partial_failure_datastore_register" and status == 502
    assert [s["status"] for s in payload["steps"]] == ["passed", "created", "failed"]
    assert payload["steps"][-1]["error_type"] == "TargetModelUnavailable"
    assert "scd2_mapping_create.groovy" not in orchestrator.order


def test_mapping_failure_does_not_generate_scenario(plan):
    orchestrator = FakeOrchestrator(
        plan,
        responses={
            "scd2_mapping_create.groovy": error_payload(
                "odi.scd2_mapping_create", "generic_absent_only_scd2_mapping_creation", "IkmUnavailable"
            )
        },
    )
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan)), approved=True
    )
    assert payload["status"] == "partial_failure_mapping_create" and status == 502
    assert "scenario_generate" not in orchestrator.order


def test_scenario_failure_is_partial(plan):
    orchestrator = FakeOrchestrator(plan, scenario_result={"status": "error", "error_type": "BoomError"})
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan)), approved=True
    )
    assert payload["status"] == "partial_failure_scenario_generate" and status == 502


def test_missing_ikm_is_imported_before_registration(plan, monkeypatch):
    calls = []
    monkeypatch.setattr(
        scd2_service.scd2_odi,
        "import_ikm",
        lambda orchestrator, approved: calls.append(approved) or {"step": "odi_ikm_import", "status": "passed"},
    )
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(ikm_present=False)})
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan)), approved=True
    )
    assert status == 200 and calls == [True]
    assert step_names(payload)[:4] == ["odi_probe", "target_ddl", "odi_ikm_import", "odi_datastore_register"]


def test_ikm_import_failure_stops(plan, monkeypatch):
    monkeypatch.setattr(
        scd2_service.scd2_odi,
        "import_ikm",
        lambda orchestrator, approved: {"step": "odi_ikm_import", "status": "failed", "error_type": "X"},
    )
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(ikm_present=False)})
    payload, _ = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan)), approved=True
    )
    assert payload["status"] == "partial_failure_ikm_import"
    assert "scd2_datastore_register.groovy" not in orchestrator.order


def test_rerun_skips_objects_that_already_exist(plan):
    orchestrator = FakeOrchestrator(
        plan,
        responses={
            "scd2_probe.groovy": probe_payload(target_datastore_exists=True, mapping_exists=True, scenario_exists=True)
        },
        scenario_result={"status": "already_ready"},
    )
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(orchestrator, ddl_log=ddl_log, ddl_handler=ddl_handler(plan, table_exists=1)),
        approved=True,
    )
    assert status == 200 and payload["status"] == "generated"
    statuses = {s["step"]: s["status"] for s in payload["steps"]}
    assert statuses["target_ddl"] == "already_exists_matching"
    assert statuses["odi_datastore_register"] == "skipped_exists"
    assert statuses["odi_mapping_create"] == "skipped_exists"
    assert statuses["odi_scenario_generate"] == "skipped_exists"
    assert not any(sql.startswith(("CREATE", "DROP", "ALTER")) for sql in ddl_log)


# ----------------------------------------------------------------------- run


RUN_BODY = {
    "source_owner": "DWH",
    "source_table": "ODS_CUSTOMERS",
    "target_owner": "DM",
    "target_table": "DIM_CUSTOMER",
    "mapping_name": "MAP_ODS_CUST_TO_DIM_CUST",
    "natural_key": ["CUST_ID"],
}


def run_catalog_handler(plan, counts):
    rows = plan_catalog_rows(plan)

    def handler(sql, params):
        if "FROM ALL_TAB_COLUMNS" in sql:
            return rows
        if "TO_CHAR(MAX" in sql:
            return [("2026-11-04",)]
        return [(counts.pop(0) if counts else 5,)]

    return handler


def test_run_executes_scenario_and_returns_count_only_postflight(plan):
    orchestrator = FakeOrchestrator(
        plan, responses={"scd2_probe.groovy": probe_payload(mapping_exists=True, scenario_exists=True, target_datastore_exists=True)}
    )
    services = make_services(orchestrator, catalog_handler=run_catalog_handler(plan, []))
    payload, status = scd2_service.run(RUN_BODY, services, approved=True)
    assert status == 200 and payload["status"] == "run_completed"
    assert payload["scenario"] == {"name": "MAP_ODS_CUST_TO_DIM_CUST_SCN", "version": "001", "context": "GLOBAL"}
    counts = payload["postflight"]["counts"]
    assert set(counts) == {
        "target_total", "target_current", "max_effective_from", "rows_with_effective_to_set",
        "duplicate_current_groups", "invalid_date_order_rows", "source_count",
    }
    assert counts["max_effective_from"] == "2026-11-04"
    assert "preflight" in payload and payload["raw_rows_included"] is False
    run_call = [c for c in orchestrator.calls if c[0] == "scenario_run"][0][1]
    assert run_call["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN"
    assert run_call["extra_allowed_scenarios"] == frozenset({"MAP_ODS_CUST_TO_DIM_CUST_SCN"})


def test_run_requires_approval(plan):
    orchestrator = FakeOrchestrator(plan)
    payload, status = scd2_service.run(RUN_BODY, make_services(orchestrator), approved=None)
    assert status == 400 and orchestrator.calls == []


def test_run_blocks_non_scd2_targets(plan):
    orchestrator = FakeOrchestrator(plan)
    plain = lambda sql, params: [("CUST_ID", "NUMBER", 22, 10, 0, "N")]  # noqa: E731
    payload, status = scd2_service.run(RUN_BODY, make_services(orchestrator, catalog_handler=plain), approved=True)
    assert status == 409 and payload["status"] == "blocked_target_not_scd2"
    assert orchestrator.calls == []


def test_run_blocks_when_scenario_does_not_exist(plan):
    orchestrator = FakeOrchestrator(plan)
    services = make_services(orchestrator, catalog_handler=run_catalog_handler(plan, []))
    payload, status = scd2_service.run(RUN_BODY, services, approved=True)
    assert status == 409 and payload["status"] == "blocked_scenario_missing"
    assert not any(c[0] == "scenario_run" for c in orchestrator.calls)


def test_run_failure_still_reports_postflight_counts(plan):
    orchestrator = FakeOrchestrator(
        plan,
        responses={"scd2_probe.groovy": probe_payload(mapping_exists=True, scenario_exists=True)},
        run_result={"status": "failed", "side_effects": True, "session": {"status": "E"}},
    )
    services = make_services(orchestrator, catalog_handler=run_catalog_handler(plan, []))
    payload, status = scd2_service.run(RUN_BODY, services, approved=True)
    assert status == 502 and payload["status"] == "run_failed"
    assert payload["postflight"]["status"] == "ok"


@pytest.mark.parametrize(
    "override",
    [{"mapping_name": "bad name"}, {"target_owner": "SYS"}, {"natural_key": []}, {"scenario_version": "1 2"}, {"context": "x;y"}],
)
def test_run_validates_identifiers(plan, override):
    payload, status = scd2_service.run({**RUN_BODY, **override}, make_services(FakeOrchestrator(plan)), approved=True)
    assert status == 400 and payload["status"] == "invalid_request"


def test_postflight_service_validates_and_returns_counts(plan):
    services = make_services(FakeOrchestrator(plan), catalog_handler=run_catalog_handler(plan, []))
    payload, status = scd2_service.postflight(
        {"owner": "dm", "table": "dim_customer", "natural_key": "cust_id"}, services
    )
    assert status == 200 and payload["counts"]["target_total"] == 5
    assert "source_count" not in payload["counts"]
    payload, status = scd2_service.postflight({"owner": "SYS", "table": "T", "natural_key": "A"}, services)
    assert status == 400
    payload, status = scd2_service.postflight({"owner": "DM", "table": "T"}, services)
    assert status == 400
