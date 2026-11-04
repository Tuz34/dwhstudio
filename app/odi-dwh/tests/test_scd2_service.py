from __future__ import annotations

import pytest

import scd2_builder
import scd2_service
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    error_payload,
    make_services,
    oracle_error,
    plan_catalog_rows,
    probe_payload,
)
from tests.test_remote_run_contract import RemoteFakeOrchestrator as FakeOrchestrator
import remote_run_contract


@pytest.fixture
def plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


def ddl_handler(plan, **kw):
    rows = plan_catalog_rows(plan)
    fail_on = kw.get("fail_on")
    users = kw.get("users", {})

    def handler(sql, params):
        if fail_on and fail_on in sql:
            return oracle_error("ORA-01031")
        if "FROM ALL_USERS" in sql:
            return [(users.get(params.get("u"), 1),)]
        if "FROM ALL_TABLES" in sql:
            return [(kw.get("table_exists", 0),)]
        if "FROM ALL_SEQUENCES" in sql:
            return [(kw.get("sequence_exists", 0),)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return rows
        if "FROM ALL_TAB_PRIVS" in sql:
            return kw.get("grants", [(g, p, t) for t, g, p in scd2_builder.required_grants(plan)])
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
        make_services(orchestrator, ddl_log=ddl_log, ddl_handler=ddl_handler(plan, table_exists=1, sequence_exists=1)),
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
    assert run_call["mapping_name"] == "MAP_ODS_CUST_TO_DIM_CUST"


def test_run_requires_approval(plan):
    orchestrator = FakeOrchestrator(plan)
    payload, status = scd2_service.run(RUN_BODY, make_services(orchestrator), approved=None)
    assert status == 400 and orchestrator.calls == []


def test_run_blocks_non_scd2_targets(plan):
    orchestrator = FakeOrchestrator(plan)
    plain = lambda sql, params: [("CUST_ID", "NUMBER", 22, 10, 0, "N", 0, None)]  # noqa: E731
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
        run_result=remote_run_contract.result("run", status="failed", stage="run", error_code="ScenarioRunFailed",
                                               agent=True, repository=True, may_have_started=True, session_status="ERROR"),
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


# ------------------------------------------------ review round 2: verification


def existing_probe(plan, **overrides):
    return probe_payload(
        plan, target_datastore_exists=True, mapping_exists=True, scenario_exists=True, **overrides
    )


def _mutated_probe(plan, mutate):
    import json as _json

    payload = _json.loads(existing_probe(plan))
    mutate(payload["result"])
    return _json.dumps(payload)


def test_matching_existing_objects_are_accepted(plan):
    orchestrator = FakeOrchestrator(
        plan, responses={"scd2_probe.groovy": existing_probe(plan)}, scenario_result={"status": "already_ready"}
    )
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1, sequence_exists=1)),
        approved=True,
    )
    assert status == 200 and payload["status"] == "generated"


def test_same_table_name_in_distinct_models_is_accepted():
    request = {**REQUEST, "target_table": REQUEST["source_table"]}
    plan = scd2_builder.build_scd2_plan(request, SOURCE_COLUMNS)
    orchestrator = FakeOrchestrator(
        plan,
        responses={"scd2_probe.groovy": probe_payload(plan, target_datastore_exists=True, mapping_exists=True, scenario_exists=True)},
        scenario_result={"status": "already_ready"},
    )
    payload, status = scd2_service.generate(
        request,
        make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1, sequence_exists=1)),
        approved=True,
    )
    assert status == 200 and payload["status"] == "generated"


def test_unrelated_scenario_name_collision_stops_before_ddl(plan):
    probe = probe_payload(plan, scenario_exists=True, scenario_versions=[], scenario_name_collision=True)
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe})
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log), approved=True
    )
    assert status == 409 and payload["status"] == "blocked_scenario_name_collision"
    assert orchestrator.order == ["scd2_probe.groovy"] and ddl_log == []


def test_generate_probes_the_planned_scenario_version(plan, monkeypatch):
    # Use a non-default version to catch a hardcoded 001 in the transport.
    plan = {**plan, "scenario_version": "007"}
    monkeypatch.setattr(scd2_service, "_build_plan", lambda *a, **kw: (plan, "test", None))
    orchestrator = FakeOrchestrator(plan)
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan)), approved=True
    )
    assert status == 200 and payload["status"] == "generated"
    assert orchestrator.calls[0][3]["odi.scd2.scenario_version"] == "007"
    assert [c[1]["version"] for c in orchestrator.calls if c[0] == "scenario_generate"] == ["007"]


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda r: r["target_columns"][2].update(role="OVERWRITE_ON_CHANGE"), "datastore_columns"),
        (lambda r: r["target_columns"].pop(), "datastore_columns"),
        (lambda r: r["target_columns"][1].update(mandatory=False), "datastore_columns"),
        (lambda r: r["target_columns"][2].update(data_format="NUMBER(8,2)", type_code="NUMBER", length=8, scale=2, length_semantics=None), "datastore_columns"),
        (lambda r: r["target_columns"][2].update(data_format="VARCHAR2(99 BYTE)", length=99), "datastore_columns"),
        (lambda r: r["target_columns"][2].update(data_format="VARCHAR2(50 CHAR)", length_semantics="CHAR"), "datastore_columns"),
        (lambda r: r["target_columns"][1].update(scale=2), "datastore_columns"),
        (lambda r: r["target_columns"][1].update(length=None), "datastore_columns"),
        (lambda r: r["target_columns"][0].update(length=10), "datastore_columns"),
        (lambda r: r["target_columns"][0].update(scale=0), "datastore_columns"),
        (lambda r: r.update(target_primary_key=["CUST_ID"]), "datastore_keys"),
        (lambda r: r.update(target_alternate_key=[]), "datastore_keys"),
        (lambda r: r["mapping"].update(source_tables=["OTHER_TABLE"]), "mapping_source"),
        (lambda r: r["mapping"].update(target_table="OTHER_DIM"), "mapping_target"),
        (lambda r: r["mapping"]["source_components"][0].update(model="TGT_DM"), "mapping_source"),
        (lambda r: r["mapping"]["target_component"].update(model="TGT_DWH"), "mapping_target"),
        (lambda r: r["mapping"].update(integration_type="IU"), "mapping_integration_type"),
        (lambda r: r["mapping"].update(target_ikm="IKM Oracle Insert"), "mapping_knowledge_modules"),
        (lambda r: r["mapping"].update(lkm_names=["LKM Other"]), "mapping_knowledge_modules"),
        (lambda r: r["mapping"].update(lkm_names=[]), "mapping_knowledge_modules"),
        (lambda r: r["mapping"].update(lkm_names=["LKM Oracle to Oracle Pull (DB Link)"]), "mapping_knowledge_modules"),
        (lambda r: r["mapping"].update(mapped_attribute_count=1), "mapping_coverage"),
    ],
)
def test_mismatched_existing_datastore_or_mapping_is_blocked_with_no_write(plan, mutate, code):
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": _mutated_probe(plan, mutate)})
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(orchestrator, ddl_log=ddl_log, ddl_handler=ddl_handler(plan, table_exists=1)),
        approved=True,
    )
    assert status == 409 and payload["status"] == "blocked_existing_object_mismatch"
    assert code in payload["mismatches"]
    assert orchestrator.order == ["scd2_probe.groovy"] and ddl_log == []


def test_existing_target_missing_sequence_and_grants_is_repaired_then_generated(plan):
    orchestrator = FakeOrchestrator(
        plan, responses={"scd2_probe.groovy": existing_probe(plan)}, scenario_result={"status": "already_ready"}
    )
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1, grants=[])),
        approved=True,
    )
    assert status == 200
    ddl_step = [s for s in payload["steps"] if s["step"] == "target_ddl"][0]
    assert ddl_step["status"] == "repaired_incomplete_target" and ddl_step["repaired"] == ["sequence", "grants"]


def test_unrepairable_existing_target_blocks_before_odi_writes(plan):
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": existing_probe(plan)})
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(
            orchestrator,
            ddl_handler=ddl_handler(plan, table_exists=1, sequence_exists=1, grants=[], fail_on="GRANT "),
        ),
        approved=True,
    )
    assert status == 409 and payload["status"] == "blocked_target_incomplete"
    assert orchestrator.order == ["scd2_probe.groovy"]


def test_incomplete_grants_on_fresh_create_stop_generation(plan):
    orchestrator = FakeOrchestrator(plan)
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(orchestrator, ddl_handler=ddl_handler(plan, fail_on="TO ODI_EXEC")),
        approved=True,
    )
    assert status == 502 and payload["status"] == "partial_failure_target_grants"
    assert orchestrator.order == ["scd2_probe.groovy"]


def run_services(plan, orchestrator):
    return make_services(orchestrator, catalog_handler=run_catalog_handler(plan, []))


def test_run_rejects_a_caller_version_that_does_not_exist_for_the_mapping(plan):
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": existing_probe(plan)})
    payload, status = scd2_service.run(
        {**RUN_BODY, "scenario_version": "999"}, run_services(plan, orchestrator), approved=True
    )
    assert status == 409 and payload["status"] == "blocked_scenario_version_missing"
    assert payload["available_versions"] == ["001"]
    assert not any(c[0] == "scenario_run" for c in orchestrator.calls)


def test_run_uses_the_verified_requested_version_or_the_latest(plan):
    probe = _mutated_probe(plan, lambda r: r.update(scenario_versions=["001", "002", "010"]))
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe})
    payload, status = scd2_service.run(RUN_BODY, run_services(plan, orchestrator), approved=True)
    assert status == 200 and payload["scenario"]["version"] == "010"
    run_call = [c for c in orchestrator.calls if c[0] == "scenario_run"][0][1]
    assert run_call["version"] == "010" and run_call["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN"
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe})
    payload, _ = scd2_service.run(
        {**RUN_BODY, "scenario_version": "002"}, run_services(plan, orchestrator), approved=True
    )
    assert payload["scenario"]["version"] == "002"


def test_run_blocks_when_the_mapping_is_not_the_requested_scd2_mapping(plan):
    probe = _mutated_probe(plan, lambda r: r["mapping"].update(integration_type="IU"))
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe})
    payload, status = scd2_service.run(RUN_BODY, run_services(plan, orchestrator), approved=True)
    assert status == 409 and payload["status"] == "blocked_existing_object_mismatch"
    assert not any(c[0] == "scenario_run" for c in orchestrator.calls)


def test_run_scenario_name_is_server_derived(plan):
    spec = scd2_service.validate_run_request({**RUN_BODY, "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN"})
    assert spec["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN"


def test_generate_blocks_on_missing_exec_user_before_any_odi_write(plan):
    orchestrator = FakeOrchestrator(plan)
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST,
        make_services(orchestrator, ddl_log=ddl_log, ddl_handler=ddl_handler(plan, users={"ODI_EXEC": 0})),
        approved=True,
    )
    assert status == 409 and payload["status"] == "blocked_grantee_missing"
    assert orchestrator.order == ["scd2_probe.groovy"]
    assert not any(sql.startswith(("CREATE", "GRANT")) for sql in ddl_log)
    assert "message" in payload["steps"][-1]


def test_km_mismatch_names_the_differing_kms_without_ids(plan):
    mutate = lambda r: r["mapping"].update(lkm_names=["LKM Oracle to Oracle Pull (DB Link)"])
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": _mutated_probe(plan, mutate)})
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1)), approved=True
    )
    assert status == 409
    km = payload["km_mismatch"]
    assert km["found_lkms"] == ["LKM Oracle to Oracle Pull (DB Link)"]
    assert km["expected_lkm"] == "LKM SQL to Oracle (Built-In)" and km["access_point_count"] == 1


def test_same_server_mapping_without_access_point_or_lkm_is_accepted(plan):
    mutate = lambda r: r["mapping"].update(lkm_names=[], access_point_count=0)
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": _mutated_probe(plan, mutate)})
    payload, _ = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1)), approved=True
    )
    assert "mapping_knowledge_modules" not in payload.get("mismatches", [])


def test_no_access_point_but_stray_lkm_is_still_a_mismatch(plan):
    from scd2_odi import mapping_mismatches
    import json as _json
    mapping = {**_json.loads(existing_probe(plan))["result"]["mapping"], "access_point_count": 0, "lkm_names": ["LKM Other"]}
    assert "mapping_knowledge_modules" in mapping_mismatches(
        mapping, {"source_model": mapping["source_components"][0]["model"], "source_table": mapping["source_tables"][0],
                  "target_model": mapping["target_component"]["model"], "target_table": mapping["target_table"]})


def test_mixed_access_points_one_builtin_one_unassigned_is_a_mismatch_naming_the_empty_lkm(plan):
    mutate = lambda r: r["mapping"].update(lkm_names=["LKM SQL to Oracle (Built-In)", ""], access_point_count=2)
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": _mutated_probe(plan, mutate)})
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_handler=ddl_handler(plan, table_exists=1)), approved=True
    )
    assert status == 409 and "mapping_knowledge_modules" in payload["mismatches"]
    assert payload["km_mismatch"]["found_lkms"] == ["LKM SQL to Oracle (Built-In)", ""]
    assert payload["km_mismatch"]["access_point_count"] == 2


def test_run_409_on_db_link_lkm_carries_km_mismatch(plan):
    probe = _mutated_probe(plan, lambda r: r["mapping"].update(lkm_names=["LKM Oracle to Oracle Pull (DB Link)"]))
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe})
    payload, status = scd2_service.run(RUN_BODY, run_services(plan, orchestrator), approved=True)
    assert status == 409 and payload["status"] == "blocked_existing_object_mismatch"
    assert payload["km_mismatch"]["found_lkms"] == ["LKM Oracle to Oracle Pull (DB Link)"]
    assert not any(c[0] == "scenario_run" for c in orchestrator.calls)
