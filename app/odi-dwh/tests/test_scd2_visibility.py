"""Offline owner-specific Type 2 integrity reads with isolated connection doubles."""

from __future__ import annotations

import pytest

import config
import scd2_builder
import scd2_service
from tests.scd2_support import (
    FakeConnection,
    REQUEST,
    SOURCE_COLUMNS,
    catalog_row,
    oracle_error,
    plan_catalog_rows,
    probe_payload,
    source_catalog_rows,
)
from tests.test_remote_run_contract import RemoteFakeOrchestrator


class Connections:
    def __init__(self, handler):
        self.instances = []
        self.handler = handler

    @property
    def log(self):
        return [sql for connection in self.instances for sql in connection.log]

    def __call__(self):
        connection = FakeConnection([], self.handler)
        self.instances.append(connection)
        return connection

    def all_closed(self):
        return bool(self.instances) and all(connection.closed for connection in self.instances)


def services(orchestrator, reader, ddl):
    return scd2_service.Scd2Services(reader, ddl, "DWH", lambda: orchestrator)


def columns(sql, params):
    if "ALL_TAB_COLUMNS" in sql:
        return source_catalog_rows()
    return []


@pytest.mark.parametrize("method", ["plan", "generate"])
@pytest.mark.parametrize("owner", ["DWH", "HR"])
def test_source_metadata_uses_own_ddl_schema_or_foreign_reader(method, owner):
    body = {**REQUEST, "source_owner": owner}
    plan = scd2_builder.build_scd2_plan(body, SOURCE_COLUMNS)
    orchestrator = RemoteFakeOrchestrator(plan, responses={
        "scd2_probe.groovy": probe_payload(plan, target_model_exists=False),
    })
    reader = Connections(columns)
    ddl = Connections(columns)
    active = services(orchestrator, reader, ddl)
    if method == "plan":
        payload, status = scd2_service.plan_preview(body, active)
        assert (status, payload["status"]) == (200, "planned")
    else:
        payload, status = scd2_service.generate(body, active, approved=True)
        assert (status, payload["status"]) == (409, "blocked_dm_model_missing")
    used, unused = (ddl, reader) if owner == "DWH" else (reader, ddl)
    assert used.all_closed() and unused.instances == []
    assert "ALL_TAB_COLUMNS" in used.log[-1]
    if owner == "DWH":
        assert used.log[0] == "SET TRANSACTION READ ONLY"
    else:
        assert "ALL_TAB_COLUMNS" in used.log[0]


@pytest.mark.parametrize("owner", ["DWH", "HR"])
def test_source_metadata_closes_selected_connection_on_query_error(owner):
    body = {**REQUEST, "source_owner": owner}
    plan = scd2_builder.build_scd2_plan(body, SOURCE_COLUMNS)
    def broken(sql, params):
        return oracle_error("synthetic catalog error") if "ALL_TAB_COLUMNS" in sql else []
    reader = Connections(broken)
    ddl = Connections(broken)
    payload, status = scd2_service.plan_preview(body, services(RemoteFakeOrchestrator(plan), reader, ddl))
    assert (status, payload["status"]) == (503, "source_metadata_unavailable")
    used, unused = (ddl, reader) if owner == "DWH" else (reader, ddl)
    assert used.all_closed() and unused.instances == []
    if owner == "DWH":
        assert used.log[0] == "SET TRANSACTION READ ONLY"
        assert "ALL_TAB_COLUMNS" in used.log[1]


def test_ddl_metadata_connection_closes_when_read_only_setup_fails():
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    reader = Connections(columns)
    ddl = Connections(lambda sql, params: oracle_error("synthetic transaction error"))
    payload, status = scd2_service.plan_preview(REQUEST, services(RemoteFakeOrchestrator(plan), reader, ddl))
    assert (status, payload["status"]) == (503, "source_metadata_unavailable")
    assert ddl.all_closed() and reader.instances == []
    assert ddl.log == ["SET TRANSACTION READ ONLY"]


def run_case(target_owner="DWH", source_owner="DWH", target_columns="scd2"):
    body = {**REQUEST, "source_owner": source_owner, "target_owner": target_owner}
    plan = scd2_builder.build_scd2_plan(body, SOURCE_COLUMNS)
    run_body = {key: body[key] for key in (
        "source_owner", "source_table", "target_owner", "target_table", "mapping_name", "natural_key"
    )}
    orchestrator = RemoteFakeOrchestrator(plan, responses={
        "scd2_probe.groovy": probe_payload(
            plan, mapping_exists=True, scenario_exists=True, target_datastore_exists=True
        ),
    })

    def handler(sql, params):
        if "ALL_TAB_COLUMNS" in sql:
            if target_columns == "empty":
                return []
            if target_columns == "plain":
                return [catalog_row("CUST_ID", "NUMBER(10)", False)]
            return plan_catalog_rows(plan)
        if "TO_CHAR(MAX" in sql:
            return [("2026-11-04",)]
        if sql.startswith("SELECT COUNT(*) FROM"):
            if sql == f'SELECT COUNT(*) FROM "{source_owner}"."ODS_CUSTOMERS"':
                return [(5,)]
            if sql in {
                f'SELECT COUNT(*) FROM "{target_owner}"."DIM_CUSTOMER"',
                f'SELECT COUNT(*) FROM "{target_owner}"."DIM_CUSTOMER" WHERE "CURRENT_FLAG"=\'1\'',
            }:
                return [(5,)]
            return [(0,)]
        return []

    return run_body, orchestrator, Connections(handler), Connections(handler)


@pytest.mark.parametrize("columns,expected", [
    ("empty", "blocked_target_not_found"),
    ("plain", "blocked_target_not_scd2"),
    ("scd2", "run_completed"),
])
def test_run_distinguishes_missing_plain_and_scd2_target(monkeypatch, columns, expected):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, ddl = run_case(target_columns=columns)
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert payload["status"] == expected
    assert status == (200 if columns == "scd2" else 409)
    assert ddl.all_closed() and reader.instances == []
    assert all(connection.log[0] == "SET TRANSACTION READ ONLY" for connection in ddl.instances)
    assert any("ALL_TAB_COLUMNS" in sql for sql in ddl.log)
    assert ("scenario_run" in orchestrator.order) is (columns == "scd2")
    assert ("scd2_probe.groovy" in orchestrator.order) is (columns == "scd2")
    if columns == "scd2":
        assert sum("ALL_TAB_COLUMNS" in sql for sql in ddl.log) == 1
        assert sum('SELECT COUNT(*) FROM "DWH"."DIM_CUSTOMER"' == sql for sql in ddl.log) == 2


def test_run_target_metadata_closes_ddl_connection_on_error(monkeypatch):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, _ = run_case()
    def broken(sql, params):
        return oracle_error("synthetic catalog error") if "ALL_TAB_COLUMNS" in sql else []
    ddl = Connections(broken)
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert (status, payload["status"]) == (503, "target_metadata_unavailable")
    assert ddl.all_closed() and reader.instances == []
    assert ddl.log[0] == "SET TRANSACTION READ ONLY"
    assert "scenario_run" not in orchestrator.order


def test_foreign_target_metadata_and_counts_use_reader_while_own_source_uses_ddl(monkeypatch):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, ddl = run_case(target_owner="DM")

    def ddl_handler(sql, params):
        if '"DM"."DIM_CUSTOMER"' in sql or "ALL_TAB_COLUMNS" in sql:
            return oracle_error("DDL user cannot see DM")
        return [(5,)] if sql == 'SELECT COUNT(*) FROM "DWH"."ODS_CUSTOMERS"' else []

    ddl.handler = ddl_handler
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert (status, payload["status"]) == (200, "run_completed")
    assert reader.all_closed() and ddl.all_closed()
    assert "ALL_TAB_COLUMNS" in reader.log[0]
    assert all('"DM"."DIM_CUSTOMER"' not in sql for sql in ddl.log)
    assert all('"DWH"."ODS_CUSTOMERS"' not in sql for sql in reader.log)
    assert all(connection.log[0] == "SET TRANSACTION READ ONLY" for connection in ddl.instances)


def test_two_foreign_owners_keep_the_reader_for_both_counts(monkeypatch):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, ddl = run_case(target_owner="DM", source_owner="HR")
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert (status, payload["status"]) == (200, "run_completed")
    assert reader.all_closed() and ddl.instances == []
    assert len(reader.instances) == 3  # metadata, one preflight, one postflight
    assert all(connection.log[0] == "SET TRANSACTION READ ONLY" for connection in reader.instances[1:])
    assert all(any('"HR"."ODS_CUSTOMERS"' in sql for sql in connection.log)
               for connection in reader.instances[1:])


def test_run_count_preflight_error_closes_ddl_connection_before_scenario(monkeypatch):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, _ = run_case()
    plan = scd2_builder.build_scd2_plan({**REQUEST, "target_owner": "DWH"}, SOURCE_COLUMNS)

    def broken(sql, params):
        if "ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(plan)
        if sql.startswith("SELECT"):
            return oracle_error("synthetic count error")
        return []

    ddl = Connections(broken)
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert (status, payload["status"]) == (503, "blocked_count_preflight")
    assert ddl.all_closed() and reader.instances == []
    assert all(connection.log[0] == "SET TRANSACTION READ ONLY" for connection in ddl.instances)
    assert "scenario_run" not in orchestrator.order


@pytest.mark.parametrize("target_owner", ["DWH", "DM"])
def test_postflight_endpoint_selects_target_owner_connection_and_closes(target_owner):
    body, orchestrator, reader, ddl = run_case(target_owner=target_owner)
    payload, status = scd2_service.postflight(
        {"owner": target_owner, "table": body["target_table"], "natural_key": "CUST_ID"},
        services(orchestrator, reader, ddl),
    )
    assert (status, payload["status"], payload["counts"]["target_total"]) == (200, "ok", 5)
    used, unused = (ddl, reader) if target_owner == "DWH" else (reader, ddl)
    assert used.all_closed() and unused.instances == []
    assert used.log[0] == "SET TRANSACTION READ ONLY"


def test_postflight_endpoint_closes_ddl_connection_on_error():
    body, orchestrator, reader, _ = run_case()
    ddl = Connections(lambda sql, params: oracle_error("synthetic count error") if sql.startswith("SELECT") else [])
    payload, status = scd2_service.postflight(
        {"owner": "DWH", "table": body["target_table"], "natural_key": "CUST_ID"},
        services(orchestrator, reader, ddl),
    )
    assert (status, payload["status"]) == (502, "error")
    assert ddl.all_closed() and reader.instances == [] and ddl.log[0] == "SET TRANSACTION READ ONLY"


def test_mixed_owner_run_counts_each_object_with_its_own_connection(monkeypatch):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, ddl = run_case(source_owner="HR")

    def reader_handler(sql, params):
        if '"HR"."ODS_CUSTOMERS"' in sql:
            return [(5,)]
        return oracle_error("reader cannot see DWH") if sql.startswith("SELECT") else []

    reader.handler = reader_handler
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert (status, payload["status"]) == (200, "run_completed")
    assert ddl.all_closed() and reader.all_closed()
    assert all(connection.log[0] == "SET TRANSACTION READ ONLY" for connection in (*ddl.instances, *reader.instances))
    assert all('"HR"."ODS_CUSTOMERS"' not in sql for sql in ddl.log)
    assert all('"DWH"."DIM_CUSTOMER"' not in sql for sql in reader.log)
    assert payload["preflight"]["counts"]["source_count"] == 5
    assert payload["postflight"]["source_current_count_equal"] is True


def test_mixed_owner_postflight_endpoint_closes_each_selected_connection():
    body, orchestrator, reader, ddl = run_case(source_owner="HR")

    def reader_handler(sql, params):
        if '"HR"."ODS_CUSTOMERS"' in sql:
            return [(5,)]
        return oracle_error("reader cannot see DWH") if sql.startswith("SELECT") else []

    reader.handler = reader_handler
    payload, status = scd2_service.postflight({
        "owner": "DWH", "table": body["target_table"], "natural_key": "CUST_ID",
        "source_owner": "HR", "source_table": body["source_table"],
    }, services(orchestrator, reader, ddl))
    assert (status, payload["status"]) == (200, "ok")
    assert reader.all_closed() and ddl.all_closed()
    assert reader.log[0] == ddl.log[0] == "SET TRANSACTION READ ONLY"
    assert all('"HR"."ODS_CUSTOMERS"' not in sql for sql in ddl.log)
    assert all('"DWH"."DIM_CUSTOMER"' not in sql for sql in reader.log)


def test_mixed_owner_count_error_closes_both_connections_before_scenario(monkeypatch):
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    body, orchestrator, reader, ddl = run_case(source_owner="HR")
    reader.handler = lambda sql, params: oracle_error("synthetic source count error") if sql.startswith("SELECT") else []
    payload, status = scd2_service.run(body, services(orchestrator, reader, ddl), approved=True)
    assert (status, payload["status"]) == (503, "blocked_count_preflight")
    assert reader.all_closed() and ddl.all_closed()
    assert "scenario_run" not in orchestrator.order
