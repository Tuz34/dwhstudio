"""Offline application-path acceptance tests for ODI-81 F1-F5."""

from __future__ import annotations

import json

import pytest

import odi81_app_acceptance as driver
import odi81_isolated_fixtures as isolated
import odi81_scd2_fixtures as fixtures
from tests.odi81_harness_support import Database

BINDINGS = json.dumps({"GLOBAL": {"DWH": "TGT_DWH", "DM": "TGT_DM"}})
FORBIDDEN = ("FIXTURE_A", "LEAK", "SELECT", "password", "101", "exec_user")


@pytest.fixture
def db():
    database = Database()
    yield database
    database.close()


def factories(db):
    return (
        {"DWH": db.factory("writer", "DWH"), "DM": db.factory("dm", "DM")},
        {"DWH": db.factory("catalog", "DWH"), "DM": db.factory("dm_reader", "DM")},
        db.factory("source", "APP"),
    )


class Client:
    def __init__(self, database: Database, flow: str) -> None:
        self.database = database
        self.flow = flow
        self.paths: list[str] = []
        self.bodies: list[dict] = []
        self.fail_on: int | None = None
        self.version = "001"
        self.context = "GLOBAL"
        self.run_status = "run_completed"
        self.corrupt_rerun = False
        self.raises = False

    def __call__(self, path: str, body: dict):
        if self.raises:
            raise RuntimeError("LEAK password=hidden")
        self.paths.append(path)
        self.bodies.append(dict(body))
        if self.fail_on == len(self.paths):
            return 503, {"status": "unknown"}
        if path == driver.GENERATE_PATH:
            self.database.install_target(self.flow, "empty")
            return 200, {"status": "generated", "scenario_version": "001"}
        run_number = sum(item == driver.RUN_PATH for item in self.paths)
        state = {1: "seed", 2: "changed", 3: "changed"}[run_number]
        self.database.install_target(self.flow, state)
        if run_number == 3 and self.corrupt_rerun:
            table = fixtures.FLOWS[self.flow]["target_table"]
            surrogate = fixtures.SK[self.flow]
            self.database.db.execute(
                f'UPDATE "{table}" SET "UPDATE_DATE"=? WHERE "{surrogate}"=2',
                ("2024-08-01T00:00:00",),
            )
        name = f"{fixtures.FLOWS[self.flow]['mapping_name']}_SCN"
        return 200, {
            "status": self.run_status,
            "scenario": {"name": name, "version": self.version, "context": self.context},
        }


def run(db, flow="F4", **kwargs):
    writers, readers, source = factories(db)
    client = kwargs.pop("http_client", None)
    if client is None:
        client = Client(db, flow)
    return client, driver.run_acceptance(
        flow, live=True, approvals=driver.full_approvals(flow), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS, **kwargs,
    )


def redacted(report):
    blob = json.dumps(report)
    for token in FORBIDDEN:
        assert token not in blob
    assert report["physical_dml_count_proven"] is False
    assert report["secret_printed"] is False


def test_default_live_and_bad_approval_call_nothing(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    blocked = driver.run_acceptance(
        "F4", approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        source_model_bindings=BINDINGS,
    )
    assert blocked["error_code"] == "LiveDisabled" and blocked["live"] is False
    assert db.events == [] and client.paths == []
    approvals = driver.full_approvals("F4")
    approvals["odi81_f4_run_3"] = False
    denied = driver.run_acceptance(
        "F4", live=True, approvals=approvals, http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        source_model_bindings=BINDINGS,
    )
    assert denied["error_code"] == "ApprovalScopeMismatch"
    assert db.events == [] and client.paths == []
    missing = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=None,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        source_model_bindings=BINDINGS,
    )
    assert missing["error_code"] == "DependencyMissing"
    assert db.events == []


def test_cli_does_not_run_live_on_import():
    assert driver.main([]) == 2
    assert driver.main(["--live"]) == 2


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_classic_requests_do_not_use_the_scd2_contract(flow):
    body = driver.classic_generate_body(flow)
    assert body["engine"] == "classic" and body["approved"] is True
    assert body["params"]["context"] == "GLOBAL"
    assert "natural_key" not in body["params"] and "exec_user" not in body
    assert driver.CLASSIC_GENERATE_PATH == "/api/mapping/generate"
    assert driver.CLASSIC_GENERATE_PATH != driver.GENERATE_PATH
    run_request = driver.classic_run_body(flow, "GLOBAL")
    assert run_request["truncate_target_table"] is (flow == "F2")
    assert run_request["scenario_version"] == "001"
    assert run_request["mapping_name"] == f"MAP_ODI81_{flow}"
    assert isolated._FLOWS[flow][0] == {
        "F1": "ODI81_SRC_F1", "F2": "ODI81_SRC_F2", "F3": "ODI81_SRC_F3",
    }[flow]


@pytest.mark.parametrize("flow", ["F4", "F5"])
def test_scd2_flow_call_order_and_snapshot(db, flow):
    client, result = run(db, flow)
    assert result["status"] == "passed", result
    assert result["calls"] == list(driver.CANONICAL_CALLS)
    assert [item["path"] for item in result["http_log"]] == [
        driver.GENERATE_PATH, driver.RUN_PATH, driver.RUN_PATH, driver.RUN_PATH,
    ]
    assert result["full_target_snapshot_equal"] is True
    assert result["rerun_semantics"] == "incremental"
    assert result["logical_business_comparison_only"] is False
    assert result["counts"]["current_rows"] == 4
    assert result["counts"]["historical_rows"] == 1
    assert result["counts"]["target_rows"] == 5
    assert result["live"] is True
    generate = client.bodies[0]
    assert "exec_user" not in generate and "source_model" not in generate and "context" not in generate
    assert generate["approved"] is True
    assert generate["natural_key"] == ["ENTITY_ID"]
    assert generate["tracked_columns"] == ["ATTR_TEXT", "VALUE_AMT"]
    assert generate["grant_select_to_app_user"] is False
    for body in client.bodies[1:]:
        assert body["scenario_version"] == "001"
        assert body["context"] == "GLOBAL"
        assert "exec_user" not in body
    redacted(result)


def test_wrong_context_stops_later_runs(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.context = "DEV"
    result = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["error_code"] == "ContextMismatch"
    assert result["downstream_stopped"] is True
    assert "app_run_2" not in result["calls"]
    assert result["calls"][-1] == "app_run_1"


def test_wrong_version_stops_later_runs(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.version = "002"
    result = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["error_code"] == "ScenarioVersionMismatch"
    assert "verify_after_r1" not in result["calls"]
    assert "app_run_2" not in result["calls"]


def test_unknown_http_stops_without_retry(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.fail_on = 2
    result = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "HttpFailed"
    assert result["downstream_stopped"] is True
    assert client.paths.count(driver.RUN_PATH) == 1
    assert "app_run_2" not in result["calls"]
    redacted(result)


def test_http_exception_is_redacted_and_stops(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.raises = True
    result = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["error_code"] == "OperationFailed"
    assert result["downstream_stopped"] is True
    assert "backup_target_r1" not in result["calls"]
    redacted(result)


def test_rerun_snapshot_difference_fails(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.corrupt_rerun = True
    result = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "SnapshotMismatch"
    assert result["full_target_snapshot_equal"] is False
    assert result["rerun_semantics"] is None
    assert result["physical_dml_count_proven"] is False
    redacted(result)


def test_missing_backup_stops_the_run(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    original = client.database.install_target

    def skip_target(flow, state):
        return None

    client.database.install_target = skip_target
    result = driver.run_acceptance(
        "F4", live=True, approvals=driver.full_approvals("F4"), http_client=client,
        writer_factories=writers, reader_factories=readers, source_reader_factory=source,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    client.database.install_target = original
    assert result["error_code"] == "BackupMissing"
    assert result["downstream_stopped"] is True
    assert "app_run_1" not in result["calls"]
    redacted(result)
