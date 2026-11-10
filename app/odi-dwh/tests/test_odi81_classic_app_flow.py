"""Classic F1-F3 application boundary. SQLite and mocks only; not a native ODI run."""

from __future__ import annotations

import json

import pytest

import mapping_generator
import odi81_app_acceptance as driver
import odi81_isolated_fixtures as isolated
from mapping_generator import MappingGenerator
from tests.test_odi81_classic_audit_expressions import evaluate_audit_rows
from mapping_run_service import MappingRunOutcome, validate_mapping_run_options
from tests.test_odi81_app_acceptance import BINDINGS
from tests.test_odi81_isolated_fixtures import Database

FORBIDDEN = ("FIXTURE_A", "LEAK", "SELECT", "password", "SECRET", "101", "exec_user")
_SOURCE_COLUMNS = (
    {"COLUMN_NAME": "ENTITY_ID", "ORACLE_TYPE": "NUMBER(10)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "ATTR_TEXT", "ORACLE_TYPE": "VARCHAR2(40 BYTE)", "IS_NULLABLE": "N"},
    {"COLUMN_NAME": "VALUE_AMT", "ORACLE_TYPE": "NUMBER(10,2)", "IS_NULLABLE": "N"},
)


def _valid_identifier(value: str) -> bool:
    text = str(value or "").strip().upper()
    return bool(text) and text.replace("_", "A").isalnum() and text[0].isalpha()


def _valid_version(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and all(character.isalnum() or character in "_.-" for character in text)


def _plan(flow: str) -> tuple[dict, dict]:
    generator = MappingGenerator()
    generator._source_columns_for = lambda _params: [dict(column) for column in _SOURCE_COLUMNS]
    validated = generator.validate_params(driver.classic_params(flow))
    return validated, generator._build_target_column_plan(validated)


def _audit(flow: str, run: int, preserved: tuple | None) -> tuple:
    if preserved is not None:
        return preserved
    stamp = f"2024-0{run}-01T00:00:00"
    if flow == "F3":
        return (stamp, stamp, "1", "2020-01-01", "2400-01-01", "SH", run)
    return (stamp, stamp, "SH", run)


class ClassicClient:
    """Apply a synthetic load only after the real validator accepts the request."""

    def __init__(self, database: Database, flow: str) -> None:
        self.database = database
        self.flow = flow
        self.paths: list[str] = []
        self.bodies: list[dict] = []
        self.fail_on: int | None = None
        self.raises = False
        self.version = driver.CLASSIC_VERSION
        self.context = "GLOBAL"
        self.corrupt_key = False
        self.skip_target = False
        self.corrupt_rerun = False

    def __call__(self, path: str, body: dict):
        if self.raises:
            raise TimeoutError("LEAK password=hidden SELECT SECRET 101")
        self.paths.append(path)
        self.bodies.append(json.loads(json.dumps(body)))
        if self.fail_on == len(self.paths):
            return 504, {"status": "unknown", "error": "SELECT SECRET 101"}
        if path == driver.CLASSIC_GENERATE_PATH:
            return self._generate(body)
        if path == driver.CLASSIC_RUN_PATH:
            return self._run(body)
        return 404, {"status": "missing"}

    def _generate(self, body: dict):
        if body.get("approved") is not True or body.get("engine") != "classic":
            return 400, {"error": "mapping generation failed."}
        validated, plan = _plan(self.flow)
        names = [name for name, _sql in plan["desired_col_defs"]]
        key_columns = list(plan["primary_key"] or [])
        key = (
            {"status": "CREATED", "columns": key_columns, "kind": "unique" if self.corrupt_key else "primary"}
            if key_columns
            else {"status": "NONE", "columns": []}
        )
        if self.corrupt_key and not key_columns:
            key = {"status": "CREATED", "columns": ["ENTITY_ID"], "kind": "primary"}
        status = "SKIPPED" if self.skip_target else "CREATED"
        if status == "CREATED":
            table = validated["tgt_table"]
            if not self.database.table_exists(table):
                # NUMBER batch ids must stay numeric; a TEXT column makes EXCEPT reject the copy.
                self.database.create(table, tuple(names), (), types={"ETL_BATCH_ID": "INTEGER"})
        return 200, {
            "overall_status": "SUCCESS",
            "params": validated,
            "steps": {"create_target_table": {
                "status": status,
                "table": f"{validated['tgt_owner']}.{validated['tgt_table']}",
                "key": key,
                "default_columns_added": list(plan["defaults_added"]),
                "ddl": "SELECT SECRET 101",
            }},
        }

    def _run(self, body: dict):
        options, error = validate_mapping_run_options(
            body, str(body.get("mapping_name") or ""), _valid_version, _valid_identifier,
        )
        if error or options is None:
            return 400, {"error": "mapping run failed."}
        run_number = sum(item == driver.CLASSIC_RUN_PATH for item in self.paths)
        self._load(options.truncate_target_table, run_number)
        if run_number == 3 and self.corrupt_rerun:
            table = driver.LEGACY[self.flow]["target_table"]
            self.database.db.execute(
                f'UPDATE "{table}" SET "UPDATE_DATE"=? WHERE "ENTITY_ID"=102',
                ("2099-01-01T00:00:00",),
            )
        return 200, {
            "status": "SUCCESS",
            "scenario": f"{options.mapping_name}_SCN",
            "version": self.version,
            "context": self.context,
            "truncate_target_table": options.truncate_target_table,
        }

    def _load(self, truncate: bool, run: int) -> None:
        source_name, target_name = isolated._FLOWS[self.flow]
        columns = isolated._BUSINESS + (isolated._EDW_EXTRA if self.flow == "F3" else isolated._ODS_EXTRA)
        profile = self._preservation()
        if profile["qualified"] is True and not truncate:
            self._load_from_expressions(source_name, target_name, columns, profile["expressions"])
            return
        source_rows = self.database.db.execute(
            f'SELECT "ENTITY_ID","ATTR_TEXT","VALUE_AMT" FROM "{source_name}" ORDER BY "ENTITY_ID"'
        ).fetchall()
        current = {
            row[0]: row
            for row in self.database.db.execute(
                f'SELECT {",".join(chr(34) + name + chr(34) for name in columns)} FROM "{target_name}"'
            ).fetchall()
        }
        loaded = []
        for entity_id, text, amount in source_rows:
            previous = current.get(entity_id)
            unchanged = previous is not None and previous[1] == text and previous[2] == amount
            preserved = previous[3:] if unchanged and not truncate else None
            loaded.append((entity_id, text, amount, *_audit(self.flow, run, preserved)))
        self._replace(target_name, columns, loaded)

    def _preservation(self) -> dict:
        generator = MappingGenerator()
        generator._source_columns_for = lambda _params: [dict(column) for column in _SOURCE_COLUMNS]
        validated = generator.validate_params(driver.classic_params(self.flow))
        plan = generator._build_target_column_plan(validated)
        return generator._prepare_audit_preservation(validated, plan)

    def _load_from_expressions(self, source_name: str, target_name: str, columns: tuple[str, ...],
                               expressions: dict[str, str]) -> None:
        ordered = [name for name in columns if name in expressions]
        rows = evaluate_audit_rows(
            self.database.db, "DWH", source_name, target_name, {name: expressions[name] for name in ordered},
            isolated._BUSINESS,
        )
        loaded = []
        for row in rows:
            business = row[:len(isolated._BUSINESS)]
            audits = dict(zip(ordered, row[len(isolated._BUSINESS):]))
            loaded.append(business + tuple(audits.get(name) for name in columns if name in expressions))
        self._replace(target_name, columns, loaded)

    def _replace(self, target_name: str, columns: tuple[str, ...], loaded: list[tuple]) -> None:
        self.database.db.execute(f'DELETE FROM "{target_name}"')
        quoted = ",".join(f'"{name}"' for name in columns)
        placeholders = ",".join("?" for _ in columns)
        self.database.db.executemany(
            f'INSERT INTO "{target_name}" ({quoted}) VALUES ({placeholders})', loaded,
        )


@pytest.fixture
def db():
    database = Database()
    yield database
    database.close()


def _run(db, flow="F1", **kwargs):
    client = kwargs.pop("http_client", None) or ClassicClient(db, flow)
    result = driver.run_acceptance(
        flow, live=True, approvals=driver.full_approvals(flow), http_client=client,
        writer_factories={"DWH": db.factory("writer")},
        reader_factories={"DWH": db.factory("catalog")},
        source_reader_factory=db.factory("source"),
        context="GLOBAL", source_model_bindings=BINDINGS, **kwargs,
    )
    return client, result


def _redacted(report) -> None:
    blob = json.dumps(report)
    for token in FORBIDDEN:
        assert token not in blob
    assert report["physical_dml_count_proven"] is False
    assert report.get("native_odi_proven") is False


@pytest.fixture(autouse=True)
def _ikm_priority(monkeypatch):
    monkeypatch.setattr(
        mapping_generator, "ODI_IKM_PRIORITY", "oracle merge,oracle update,oracle insert",
    )


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_real_builder_locks_mode_key_and_audit_shape(flow):
    validated, plan = _plan(flow)
    spec = driver.LEGACY[flow]
    names = [name for name, _sql in plan["desired_col_defs"]]
    assert validated["integration_type"] == spec["integration_type"]
    assert validated["context"] == "GLOBAL"
    assert validated["drop_target_if_exists"] is False
    assert validated["needs_skey"] is False
    assert names == ["ENTITY_ID", "ATTR_TEXT", "VALUE_AMT", *driver._audit_names(flow)]
    assert 'NUMBER(10)' in plan["col_defs"][0] and "NOT NULL" in plan["col_defs"][0]
    if flow == "F2":
        assert plan["primary_key"] is None and plan["unique_key"] is None
        assert "insert" in validated["ikm_name"].lower()
        assert "merge" not in validated["ikm_name"].lower()
    else:
        assert plan["primary_key"] == ["ENTITY_ID"]
        assert "merge" in validated["ikm_name"].lower() or "update" in validated["ikm_name"].lower()


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_classic_flow_reaches_run3_with_mode_specific_witness(db, flow):
    client, result = _run(db, flow)
    assert result["status"] == "passed", result
    assert result["calls"] == list(driver.LEGACY_CALLS)
    assert [item["path"] for item in result["http_log"]] == [
        driver.CLASSIC_GENERATE_PATH, driver.CLASSIC_RUN_PATH,
        driver.CLASSIC_RUN_PATH, driver.CLASSIC_RUN_PATH,
    ]
    assert driver.GENERATE_PATH not in client.paths
    assert client.bodies[0]["engine"] == "classic"
    assert client.bodies[0]["params"]["update_key_columns"] == ([] if flow == "F2" else ["ENTITY_ID"])
    assert all(body["truncate_target_table"] is (flow == "F2") for body in client.bodies[1:])
    assert all(body["context"] == "GLOBAL" and body["scenario_version"] == "001" for body in client.bodies[1:])
    assert result["logical_business_comparison_only"] is (flow == "F2")
    if flow == "F2":
        assert result["full_target_snapshot_equal"] is False
        assert result["rerun_semantics"] == "full_reload"
        table = driver.LEGACY[flow]["target_table"]
        batches = {row[0] for row in db.db.execute(f'SELECT "ETL_BATCH_ID" FROM "{table}"')}
        assert batches == {3}
    else:
        assert result["full_target_snapshot_equal"] is True
        assert result["rerun_semantics"] == "incremental"
        table = driver.LEGACY[flow]["target_table"]
        batches = {row[0] for row in db.db.execute(f'SELECT "ETL_BATCH_ID" FROM "{table}"')}
        sources = {row[0] for row in db.db.execute(f'SELECT "SOURCE_SYSTEM" FROM "{table}"')}
        assert batches == {'<%=odiRef.getSession("SESS_NO")%>'}
        assert sources == {mapping_generator._SOURCE_SYSTEM_LITERAL}
        if flow == "F3":
            flags = {row[0] for row in db.db.execute(f'SELECT "CURRENT_FLAG" FROM "{table}"')}
            ends = {row[0] for row in db.db.execute(f'SELECT "EFFECTIVE_TO" FROM "{table}"')}
            assert flags == {"Y"}
            assert ends == {"9999-12-31"}
    _redacted(result)


def test_run3_audit_drift_is_not_a_noop(db):
    client = ClassicClient(db, "F1")
    client.corrupt_rerun = True
    _client, result = _run(db, "F1", http_client=client)
    assert result["status"] == "failed"
    assert result["stage"] == "verify_after_r3"
    assert "app_run_3" in result["calls"]
    assert result["error_code"] == "SnapshotMismatch"
    assert result["full_target_snapshot_equal"] is False
    assert result["rerun_semantics"] is None
    _redacted(result)


def test_skipped_generation_is_not_completion(db):
    client = ClassicClient(db, "F1")
    client.skip_target = True
    _client, result = _run(db, "F1", http_client=client)
    assert result["status"] == "failed"
    assert result["error_code"] == "HttpFailed"
    assert result["downstream_stopped"] is True
    assert "backup_target_r1" not in result["calls"]
    _redacted(result)


def test_wrong_key_in_the_generate_response_blocks_before_backup(db):
    client = ClassicClient(db, "F1")
    client.corrupt_key = True
    _client, result = _run(db, "F1", http_client=client)
    assert result["error_code"] == "TargetPrimaryKeyMismatch"
    assert "backup_target_r1" not in result["calls"]
    assert result["http_write_attempted"] is True
    _redacted(result)


def test_timeout_stays_unknown_and_stops(db):
    client = ClassicClient(db, "F3")
    client.raises = True
    _client, result = _run(db, "F3", http_client=client)
    assert result["error_code"] == "OperationFailed"
    assert result["outcome_unknown"] is True
    assert result["manual_review_required"] is True
    assert result["downstream_stopped"] is True
    assert "app_run_1" not in result["calls"]
    _redacted(result)


def test_http_504_stops_without_a_later_run(db):
    client = ClassicClient(db, "F1")
    client.fail_on = 2
    _client, result = _run(db, "F1", http_client=client)
    assert result["error_code"] == "HttpFailed"
    assert result["outcome_unknown"] is True
    assert result["manual_review_required"] is True
    assert client.paths.count(driver.CLASSIC_RUN_PATH) == 1
    assert "app_run_2" not in result["calls"]
    _redacted(result)


def test_missing_run3_approval_and_non_global_context_call_nothing(db):
    approvals = driver.full_approvals("F1")
    approvals["odi81_f1_verify_before_r3"] = False
    client = ClassicClient(db, "F1")
    denied = driver.run_acceptance(
        "F1", live=True, approvals=approvals, http_client=client,
        writer_factories={"DWH": db.factory("writer")},
        reader_factories={"DWH": db.factory("catalog")},
        source_reader_factory=db.factory("source"),
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert denied["error_code"] == "ApprovalScopeMismatch"
    assert db.events == [] and client.paths == []
    other = driver.run_acceptance(
        "F1", live=True, approvals=driver.full_approvals("F1"), http_client=client,
        writer_factories={"DWH": db.factory("writer")},
        reader_factories={"DWH": db.factory("catalog")},
        source_reader_factory=db.factory("source"),
        context="DEV", source_model_bindings=json.dumps({"DEV": {"DWH": "TGT_DWH"}}),
    )
    assert other["error_code"] == "ContextMismatch"
    assert db.events == [] and client.paths == []


def test_preexisting_backup_drift_is_left_untouched(db):
    db.provision("F1", state="seed", run=2)
    db.db.execute('UPDATE "ODI81N_F1_T_R2_BKP" SET "ATTR_TEXT"=? WHERE "ENTITY_ID"=101', ("DRIFT",))
    before = db.db.execute('SELECT "ATTR_TEXT" FROM "ODI81N_F1_T_R2_BKP" WHERE "ENTITY_ID"=101').fetchone()[0]
    result = isolated.create_retained_backup(
        "F1", "target", 2, db.factory("writer"), approved=True,
        approval_scope="odi81_f1_backup_target_r2",
    )
    assert result["status"] != "passed"
    assert result.get("created") is not True
    after = db.db.execute('SELECT "ATTR_TEXT" FROM "ODI81N_F1_T_R2_BKP" WHERE "ENTITY_ID"=101').fetchone()[0]
    assert after == before
    assert "DRIFT" not in json.dumps(result)


def test_classic_generate_route_forwards_params_and_redacts(app_client, monkeypatch):
    import app as app_module

    seen: dict = {}

    class FakeGenerator:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def generate_from_params(self, params):
            seen["params"] = params
            return {"overall_status": "SUCCESS", "params": params, "error": "password=secret SELECT 101"}

        def generate_from_prompt(self, _prompt):
            raise AssertionError("prompt path")

    def reject_graph(**_kwargs):
        raise AssertionError("graph")

    monkeypatch.setattr(app_module, "MappingGenerator", FakeGenerator)
    monkeypatch.setattr(app_module, "run_mapping_graph", reject_graph)
    denied = app_client.post("/api/mapping/generate", json={"engine": "classic", "params": driver.classic_params("F1")})
    assert denied.status_code == 400 and seen == {}
    response = app_client.post("/api/mapping/generate", json=driver.classic_generate_body("F1"))
    payload = response.get_json()
    assert response.status_code == 200
    assert seen["params"]["integration_type"] == "IU"
    assert seen["params"]["update_key_columns"] == ["ENTITY_ID"]
    assert seen["params"]["tgt_table"] == "ODS_ODI81N_F1"
    assert seen["params"]["context"] == "GLOBAL"
    blob = json.dumps(payload)
    assert "password" not in blob and "101" not in blob and "SELECT" not in blob
    assert payload["error"] == "mapping generation failed."


def test_classic_run_route_passes_version_context_and_truncate(app_client, monkeypatch):
    import routes.mapping_write as mapping_write

    seen: dict = {}

    def fake_execute(_app, options, _phase=None):
        seen["options"] = options
        return MappingRunOutcome({
            "status": "SUCCESS",
            "scenario": f"{options.mapping_name}_SCN",
            "version": options.version,
            "context": options.context,
            "truncate_target_table": options.truncate_target_table,
        }, 200)

    monkeypatch.setattr(mapping_write, "execute_mapping_run", fake_execute)
    denied = app_client.post("/api/mapping/run", json={"mapping_name": "MAP_ODI81N_F2", "truncate_target_table": True})
    assert denied.status_code == 400 and seen == {}
    response = app_client.post("/api/mapping/run", json=driver.classic_run_body("F2", "GLOBAL"))
    assert response.status_code == 200
    assert seen["options"].version == "001"
    assert seen["options"].context == "GLOBAL"
    assert seen["options"].truncate_target_table is True
    assert seen["options"].mapping_name == "MAP_ODI81N_F2"
    incremental = app_client.post("/api/mapping/run", json=driver.classic_run_body("F3", "GLOBAL"))
    assert incremental.status_code == 200
    assert seen["options"].truncate_target_table is False
    assert seen["options"].mapping_name == "MAP_ODI81N_F3"


def test_actual_run_service_success_satisfies_classic_driver_contract(monkeypatch):
    from types import SimpleNamespace
    import mapping_run_service as service
    import odi_orchestrator
    from tests.test_api_endpoints import _RemoteRunDouble

    class Orchestrator(_RemoteRunDouble):
        def update_mapping(self, **_kwargs):
            return "SUCCESS: options updated"

    monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", Orchestrator)
    monkeypatch.setattr(service, "_persist_run_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_create_failure_alert", lambda *args, **kwargs: None)
    options = service.MappingRunOptions("MAP_ODI81N_F1", "001", "GLOBAL", False)
    with odi_orchestrator.groovy_approval_context(True):
        outcome = service.execute_mapping_run(SimpleNamespace(), options)
    assert outcome.status_code == 200 and outcome.payload["status"] == "SUCCESS"
    driver._accept_classic_run("F1", "GLOBAL", outcome.status_code, outcome.payload)
