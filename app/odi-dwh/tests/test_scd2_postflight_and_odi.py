from __future__ import annotations

import re

import pytest

import scd2_builder
import scd2_odi
import scd2_postflight
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    connection_factory,
    error_payload,
    mapping_payload,
    probe_payload,
    register_payload,
)
from tests.test_remote_run_contract import RemoteFakeOrchestrator as FakeOrchestrator


@pytest.fixture
def plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


# ---------------------------------------------------------------- postflight


def test_postflight_sql_is_count_only():
    statements = scd2_postflight.build_postflight_statements(
        "DM", "DIM_CUSTOMER", ["CUST_ID"], source=("DWH", "ODS_CUSTOMERS")
    )
    assert set(statements) == {
        "target_total",
        "target_current",
        "max_effective_from",
        "rows_with_effective_to_set",
        "duplicate_current_groups",
        "invalid_date_order_rows",
        "source_count",
    }
    for name, sql in statements.items():
        assert sql.startswith("SELECT"), name
        assert re.search(r"COUNT\(\*\)|MAX\(", sql), name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE)\b", sql), name
    assert "TO_CHAR(MAX(\"EFFECTIVE_FROM\"), 'YYYY-MM-DD')" in statements["max_effective_from"]
    # No statement projects a business or key column value.
    for name, sql in statements.items():
        projection = sql.split(" FROM ")[0]
        assert "CUST_ID" not in projection and "CUST_NAME" not in projection, name


def test_postflight_rejects_unsafe_identifiers_and_owner():
    with pytest.raises(scd2_builder.Scd2PlanError):
        scd2_postflight.build_postflight_statements("SYS", "DIM_CUSTOMER", ["CUST_ID"])
    with pytest.raises(scd2_builder.Scd2PlanError):
        scd2_postflight.build_postflight_statements("DM", 'X" OR 1=1 --', ["CUST_ID"])
    with pytest.raises(scd2_builder.Scd2PlanError):
        scd2_postflight.build_postflight_statements("DM", "DIM_CUSTOMER", ["A,B"])
    with pytest.raises(scd2_builder.Scd2PlanError):
        scd2_postflight.build_postflight_statements("DM", "DIM_CUSTOMER", [])
    with pytest.raises(scd2_builder.Scd2PlanError):
        scd2_postflight.build_postflight_statements("DM", "DIM_CUSTOMER", ["CUST_ID"], source=("SYS", "T"))


def test_collect_postflight_returns_integers_and_one_date_string():
    def handler(sql, params):
        if "TO_CHAR(MAX" in sql:
            return [("2026-11-04",)]
        return [(7,)]

    log = []
    result = scd2_postflight.collect_postflight(
        "DM", "DIM_CUSTOMER", ["CUST_ID"], connection_factory(log, handler), source=("DWH", "ODS_CUSTOMERS")
    )
    assert result["status"] == "ok"
    assert result["counts"]["max_effective_from"] == "2026-11-04"
    assert result["counts"]["target_total"] == 7 and result["counts"]["source_count"] == 7
    assert result["raw_rows_included"] is False and result["pk_values_included"] is False
    assert all(isinstance(v, int) for k, v in result["counts"].items() if k != "max_effective_from")


def test_collect_postflight_database_error_is_status_only():
    import oracledb

    def handler(sql, params):
        return oracledb.DatabaseError("ORA-00942: detail")

    result = scd2_postflight.collect_postflight("DM", "DIM_CUSTOMER", ["CUST_ID"], connection_factory([], handler))
    assert result["status"] == "error" and result["counts"] == {}
    assert "ORA-00942" not in str(result)


# ------------------------------------------------------------------ ODI side


def test_probe_normalizes_booleans_and_counts(plan):
    orchestrator = FakeOrchestrator(plan)
    params = scd2_builder.probe_params("DWH", "ODS_CUSTOMERS", "DM", "DIM_CUSTOMER", "MAP_X")
    step = scd2_odi.probe(orchestrator, params)
    assert step["status"] == "passed" and step["side_effects"] is False
    assert step["checks"]["target_model_exists"] is True
    assert step["source_column_count"] == 4
    assert orchestrator.calls[0][2] is False  # read-only: no approval context needed


def test_register_and_mapping_run_only_with_approval_context(plan):
    orchestrator = FakeOrchestrator(plan)
    assert scd2_odi.register_datastore(orchestrator, plan, True)["status"] == "passed"
    assert scd2_odi.create_mapping(orchestrator, plan, True)["status"] == "passed"
    assert [c[2] for c in orchestrator.calls] == [True, True]
    # Without approval the real run_groovy refuses; the context is False here.
    orchestrator2 = FakeOrchestrator(plan)
    scd2_odi.register_datastore(orchestrator2, plan, None)
    assert orchestrator2.calls[0][2] is False


def test_groovy_parameters_come_from_the_plan_only(plan):
    orchestrator = FakeOrchestrator(plan)
    scd2_odi.create_mapping(orchestrator, plan, True)
    script, params = orchestrator.calls[0][1], orchestrator.calls[0][3]
    assert script == "scd2_mapping_create.groovy"
    assert params["odi.scd2.mapping"] == "MAP_ODS_CUST_TO_DIM_CUST"
    assert params["odi.scd2.target_model"] == "TGT_DM"
    assert set(params) >= {"odi.project", "odi.folder", "odi.scd2.sequence", "odi.scd2.sk"}


@pytest.mark.parametrize(
    "output, expected",
    [
        (error_payload("odi.scd2_mapping_create", "generic_absent_only_scd2_mapping_creation", "IkmUnavailable"), "IkmUnavailable"),
        ("Hata: ODI Groovy action failed. error_type=GroovyProcessError", "GroovyProcessError"),
        ("not json", "InvalidGroovyOutput"),
        ('{"tool": "odi.other", "status": "passed"}', "InvalidGroovyEnvelope"),
    ],
)
def test_error_envelopes_become_failed_steps_without_raw_output(plan, output, expected):
    orchestrator = FakeOrchestrator(plan, responses={"scd2_mapping_create.groovy": output})
    step = scd2_odi.create_mapping(orchestrator, plan, True)
    assert step["status"] == "failed" and step["error_type"] == expected
    assert "output" not in step and "stdout" not in step


def test_envelope_with_unexpected_counts_is_rejected(plan):
    bad = mapping_payload(plan).replace(
        f'"mapped_attribute_count": {len(plan["target_columns"])}', '"mapped_attribute_count": 3'
    )
    orchestrator = FakeOrchestrator(plan, responses={"scd2_mapping_create.groovy": bad})
    assert scd2_odi.create_mapping(orchestrator, plan, True)["status"] == "failed"
    orchestrator = FakeOrchestrator(plan, responses={"scd2_datastore_register.groovy": register_payload(plan).replace('"commit_completed": true', '"commit_completed": false')})
    assert scd2_odi.register_datastore(orchestrator, plan, True)["status"] == "failed"


def test_scenario_generation_statuses(plan):
    assert scd2_odi.generate_scenario(FakeOrchestrator(plan), plan, True)["status"] == "passed"
    already = FakeOrchestrator(plan, scenario_result={"status": "already_ready"})
    assert scd2_odi.generate_scenario(already, plan, True)["status"] == "skipped_exists"
    failed = FakeOrchestrator(plan, scenario_result={"status": "error", "error_type": "ScenarioError"})
    step = scd2_odi.generate_scenario(failed, plan, True)
    assert step["status"] == "failed" and step["error_type"] == "ScenarioError"
    call = FakeOrchestrator(plan)
    scd2_odi.generate_scenario(call, plan, True)
    kwargs = call.calls[0][1]
    assert kwargs["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN" and kwargs["version"] == "001"


def test_run_scenario_passes_exact_mapping_and_scenario_to_remote_runner(plan):
    orchestrator = FakeOrchestrator(plan)
    step = scd2_odi.run_scenario(orchestrator, plan["scenario_name"], "001", "GLOBAL", True)
    assert step["status"] == "passed"
    kwargs = orchestrator.calls[0][1]
    assert kwargs["mapping_name"] == "MAP_ODS_CUST_TO_DIM_CUST"
    assert kwargs["scenario_name"] == "MAP_ODS_CUST_TO_DIM_CUST_SCN"
    assert kwargs["approved"] is True


def test_probe_payload_helper_is_strictly_validated(plan):
    broken = probe_payload().replace('"lkm_present": true', '"lkm_present": "yes"')
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": broken})
    params = scd2_builder.probe_params("DWH", "ODS_CUSTOMERS", "DM", "DIM_CUSTOMER", "MAP_X")
    assert scd2_odi.probe(orchestrator, params)["status"] == "failed"


@pytest.mark.parametrize(
    "data_format, length, scale, matches",
    [
        ("NUMBER", None, None, True),
        ("NUMBER", 10, None, False),
        ("NUMBER", None, 0, False),
        ("NUMBER", None, 2, False),
        ("NUMBER", 0, None, False),
        ("NUMBER(10)", 10, None, True),
        ("NUMBER(10)", 10, 0, True),
        ("NUMBER(10)", 10, 2, False),
        ("NUMBER(10)", None, None, False),
        ("NUMBER(10)", 9, 0, False),
        ("NUMBER(10,0)", 10, None, True),
        ("NUMBER(10,0)", 10, 0, True),
        ("NUMBER(10,2)", 10, 2, True),
        ("NUMBER(10,2)", 10, None, False),
        ("NUMBER(10,2)", 10, 0, False),
        ("NUMBER(10,2)", 9, 2, False),
        ("FLOAT", None, None, True),
        ("FLOAT", 126, None, True),
        ("FLOAT", 10, None, False),
        ("FLOAT", 126, 0, False),
        ("FLOAT(10)", 10, None, True),
        ("FLOAT(10)", None, None, False),
    ],
)
def test_numeric_metadata_defaults_are_checked_independently_of_format(data_format, length, scale, matches):
    expected = {"name": "N", "role": "NONE", "mandatory": False, "data_format": data_format}
    actual = {
        **expected,
        "type_code": data_format.split("(", 1)[0],
        "length": length,
        "scale": scale,
        "length_semantics": None,
    }
    assert scd2_odi._column_matches(actual, expected) is matches
