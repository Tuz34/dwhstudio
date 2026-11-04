"""
API Endpoint Tests
━━━━━━━━━━━━━━━━━━━━━

Verifies the HTTP status and JSON structure of the core API endpoints.
"""

import pytest
import json
import os
import pandas as pd
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _RemoteRunDouble:
    def remote_agent_health(self, **kwargs):
        from tests.test_remote_run_contract import healthy
        return healthy()

    def generate_scenario_metadata(self, **kwargs):
        from tests.test_remote_run_contract import generated
        return generated(kwargs["scenario_name"], kwargs["version"])

    def run_scenario_remote(self, **kwargs):
        from tests.test_remote_run_contract import done
        return done()


class TestHttpSecurityContracts:
    def test_app_post_endpoints_do_not_bypass_csrf(self):
        content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")

        assert "@csrf.exempt" not in content

    def test_debug_mode_is_explicit_opt_in(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        config_content = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")

        assert 'APP_HOST          = os.getenv("APP_HOST", "127.0.0.1")' in config_content
        assert "APP_DEBUG" in config_content
        assert "debug=APP_DEBUG" in app_content
        assert 'debug=(ENVIRONMENT == "dev")' not in app_content

    def test_execute_dml_requires_backend_approval(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "execute_dml",
            lambda *_args, **_kwargs: pytest.fail("DML must not run without approved:true"),
        )

        resp = app_client.post(
            "/api/execute",
            json={"sql": "UPDATE DWH.ODS_HR_EMPLOYEES SET FIRST_NAME = FIRST_NAME"},
        )

        assert resp.status_code == 400
        assert "approved" in resp.get_json()["error"].lower()

    def test_mapping_write_endpoints_require_backend_approval(self, app_client, monkeypatch):
        import app as app_module

        class FakeMappingGenerator:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def generate_from_params(self, *_args, **_kwargs):
                pytest.fail("mapping generation must not run without approved:true")

            def update_mapping(self, *_args, **_kwargs):
                pytest.fail("mapping update must not run without approved:true")

            def delete_mapping(self, *_args, **_kwargs):
                pytest.fail("mapping delete must not run without approved:true")

        monkeypatch.setattr(app_module, "MappingGenerator", FakeMappingGenerator)

        cases = [
            ("/api/mapping/generate", {"params": {"src_table": "EMPLOYEES", "tgt_table": "ODS_HR_EMPLOYEES"}}),
            ("/api/mapping/update", {"mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "changes": {"tgt_owner": "DWH"}}),
            ("/api/mapping/delete", {"mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "tgt_owner": "DWH"}),
        ]

        for url, payload in cases:
            resp = app_client.post(url, json=payload)
            assert resp.status_code == 400
            assert "approved" in resp.get_json()["error"].lower()


class TestIncrementalExecutionEndpoint:
    def _ready_preflight(self):
        result = {
            "spec": "SH_SALES_ODS",
            "strategy": "incremental_append_with_watermark",
            "will_run_incremental": False,
            "raw_rows_included": False,
            "secret_printed": False,
            "dry_run_delta": {"estimated_new_rows": 1},
            "rerun_guard": {
                "state": "new_window_ready",
                "can_append_without_duplicate": True,
            },
            "execution_plan": {"status": "awaiting_explicit_approval"},
        }
        return {
            "tool": "odi.incremental_strategy_preflight",
            "status": "passed",
            "mode": "dry_run_read_only",
            "raw_rows_included": False,
            "secret_printed": False,
            "results": [result],
        }

    def test_incremental_execute_requires_backend_approval_before_write_connection(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "run_configured_incremental_strategy_preflight",
            lambda _specs: self._ready_preflight(),
            raising=False,
        )
        monkeypatch.setattr(
            app_module,
            "_open_incremental_target_connection",
            lambda: pytest.fail("write connection must not open without approved:true"),
            raising=False,
        )
        monkeypatch.setattr(
            app_module,
            "execute_incremental_append",
            lambda *_args, **_kwargs: pytest.fail("executor must not run without approved:true"),
            raising=False,
        )

        resp = app_client.post("/api/incremental/execute", json={"spec_name": "SH_SALES_ODS"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["execution"]["status"] == "blocked_until_explicit_approval"
        assert data["execution"]["will_execute"] is False
        assert data["raw_rows_included"] is False

    def test_incremental_execute_calls_executor_with_redacted_status_only(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeConnection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeWatermarkManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        from routes import schema_write

        fake_conn = FakeConnection()
        fake_source = FakeConnection()
        monkeypatch.setattr(
            app_module,
            "run_configured_incremental_strategy_preflight",
            lambda _specs: self._ready_preflight(),
            raising=False,
        )
        monkeypatch.setattr(app_module, "_open_incremental_target_connection", lambda: fake_conn, raising=False)
        monkeypatch.setattr(app_module, "WatermarkManager", FakeWatermarkManager)

        def fake_execute(preflight_result, *, approved, target_conn, source_conn, watermark_writer):
            calls.append({
                "approved": approved,
                "target_conn": target_conn,
                "source_conn": source_conn,
                "watermark_writer": watermark_writer,
                "spec": preflight_result["spec"],
            })
            return {
                "status": "executed",
                "will_execute": True,
                "rows_appended": 1,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
            }

        monkeypatch.setattr(app_module, "execute_incremental_append", fake_execute, raising=False)
        monkeypatch.setattr(schema_write, "_open_incremental_source_connection", lambda: fake_source, raising=False)

        resp = app_client.post(
            "/api/incremental/execute",
            json={"spec_name": "SH_SALES_ODS", "approved": True},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["execution"]["status"] == "executed"
        assert data["execution"]["raw_rows_included"] is False
        assert data["execution"]["pk_values_included"] is False
        assert calls == [{
            "approved": True,
            "target_conn": fake_conn,
            "source_conn": fake_source,
            "watermark_writer": calls[0]["watermark_writer"],
            "spec": "SH_SALES_ODS",
        }]
        assert fake_conn.closed is True
        assert fake_source.closed is True

    def test_incremental_execute_redacts_executor_exception_message(self, app_client, monkeypatch):
        import app as app_module

        class FakeConnection:
            def close(self):
                pass

        class FakeWatermarkManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        monkeypatch.setattr(
            app_module,
            "run_configured_incremental_strategy_preflight",
            lambda _specs: self._ready_preflight(),
            raising=False,
        )
        from routes import schema_write

        monkeypatch.setattr(app_module, "_open_incremental_target_connection", lambda: FakeConnection(), raising=False)
        monkeypatch.setattr(schema_write, "_open_incremental_source_connection", lambda: FakeConnection(), raising=False)
        monkeypatch.setattr(app_module, "WatermarkManager", FakeWatermarkManager)

        def boom(*_args, **_kwargs):
            raise RuntimeError("internal host credential detail must not leak")

        monkeypatch.setattr(app_module, "execute_incremental_append", boom, raising=False)

        resp = app_client.post(
            "/api/incremental/execute",
            json={"spec_name": "SH_SALES_ODS", "approved": True},
        )

        assert resp.status_code == 500
        body = json.dumps(resp.get_json())
        assert "RuntimeError" in body
        assert "internal host credential detail" not in body


class TestMappingRunEndpoint:
    """POST /api/mapping/run safety contract tests."""

    def test_mapping_run_requires_truncate_choice(self, app_client, monkeypatch):
        import odi_orchestrator
        import subprocess

        class FakeOdiOrchestrator:
            def generate_scenario(self, *_args, **_kwargs):
                return "OK"

            def run_scenario(self, *_args, **_kwargs):
                return "OK"

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("mapping run must not use subprocess without truncate choice"),
        )

        resp = app_client.post(
            "/api/mapping/run",
            json={"mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "approved": True},
        )

        assert resp.status_code == 400
        assert "truncate_target_table" in resp.get_json()["error"]

    def test_mapping_run_redacts_unhandled_exception_message(self, app_client, monkeypatch):
        import odi_orchestrator

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def update_mapping(self, **_kwargs):
                raise RuntimeError("internal credential detail must not leak")

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/mapping/run",
            json={
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "truncate_target_table": True,
                "scenario_version": "001",
                "approved": True,
            },
        )

        assert resp.status_code == 500
        body = json.dumps(resp.get_json())
        assert "RuntimeError" in body
        assert "mapping run failed" in body.lower()
        assert "internal credential detail" not in body

    def test_mapping_run_applies_user_truncate_choice_before_orchestrator_run(self, app_client, monkeypatch):
        import app as app_module
        import odi_orchestrator
        import subprocess

        calls = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def upsert_state(self, *_args, **_kwargs):
                return None

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def remote_agent_health(self, **kwargs):
                calls.append(("health", kwargs))
                return super().remote_agent_health(**kwargs)

            def update_mapping(self, **kwargs):
                calls.append(("update_mapping", kwargs))
                return "SUCCESS: options updated"

            def generate_scenario_metadata(self, **kwargs):
                calls.append(("generate_scenario", kwargs))
                return super().generate_scenario_metadata(**kwargs)

            def run_scenario_remote(self, **kwargs):
                calls.append(("run_scenario", kwargs))
                return super().run_scenario_remote(**kwargs)

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)
        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("mapping run must use OdiOrchestrator.run_scenario"),
        )

        resp = app_client.post(
            "/api/mapping/run",
            json={
                "mapping_name": "map_hr_employees_to_ods",
                "truncate_target_table": True,
                "scenario_version": "001",
                "context": "GLOBAL",
                "approved": True,
            },
        )

        assert resp.status_code == 200
        assert calls == [
            ("health", {"approved": True}),
            (
                "update_mapping",
                {
                    "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                    "operation": "update_km",
                    "truncate_target_table": True,
                },
            ),
            ("generate_scenario", {"approved": True, "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN",
                                   "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "version": "001"}),
            ("run_scenario", {"approved": True, "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN",
                              "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "version": "001", "context": "GLOBAL"}),
        ]

    def test_mapping_run_treats_fatal_generation_output_as_failure(self, app_client, monkeypatch):
        import app as app_module
        import odi_orchestrator

        states = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def upsert_state(self, state, thread_id=None):
                states.append((state, thread_id))

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def update_mapping(self, **_kwargs):
                return "SUCCESS: options updated"

            def generate_scenario_metadata(self, **_kwargs):
                return {"status": "error"}

            def run_scenario_remote(self, **_kwargs):
                pytest.fail("run_scenario must not be called after generation failure")

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)
        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/mapping/run",
            json={
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "truncate_target_table": True,
                "approved": True,
            },
        )

        body = resp.get_json()
        assert resp.status_code == 400
        assert "Scenario generation failed" in body["error"]
        assert body["batch_id"].startswith("RUN-")
        assert states[-1][0]["overall_status"] == "RUN_FAILED"
        assert states[-1][0]["current_step"] == "generate_scenario"

    def test_mapping_run_records_success_state_with_real_batch_id(self, app_client, monkeypatch):
        import app as app_module
        import odi_orchestrator

        states = []
        alerts = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def upsert_state(self, state, thread_id=None):
                states.append((state, thread_id))

        class FakeAlertManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def create_alert(self, **kwargs):
                alerts.append(kwargs)
                return 101

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def update_mapping(self, **_kwargs):
                return "SUCCESS: options updated"

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)
        monkeypatch.setattr(app_module, "AlertManager", FakeAlertManager)
        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/mapping/run",
            json={
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "truncate_target_table": True,
                "scenario_version": "001",
                "context": "GLOBAL",
                "approved": True,
            },
        )

        body = resp.get_json()
        assert resp.status_code == 200
        assert body["batch_id"].startswith("RUN-")
        assert body["batch_id"] != "RUN-LIVE"
        assert states[-1][0]["batch_id"] == body["batch_id"]
        assert states[-1][0]["mapping_name"] == "MAP_HR_EMPLOYEES_TO_ODS"
        assert states[-1][0]["overall_status"] == "SUCCEEDED"
        assert states[-1][0]["current_step"] == "run_scenario"
        assert states[-1][0]["completed_steps"] == [
            "update_km",
            "generate_scenario",
            "run_scenario",
        ]
        assert alerts == []

    def test_mapping_run_creates_single_alert_on_terminal_failure(self, app_client, monkeypatch):
        import app as app_module
        import odi_orchestrator

        states = []
        alerts = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def upsert_state(self, state, thread_id=None):
                states.append((state, thread_id))

        class FakeAlertManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def create_alert(self, **kwargs):
                alerts.append(kwargs)
                return 202

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def update_mapping(self, **_kwargs):
                return "SUCCESS: options updated"

            def run_scenario_remote(self, **_kwargs):
                import remote_run_contract
                return remote_run_contract.result("run", status="failed", stage="run", error_code="ScenarioRunFailed",
                                                   agent=True, repository=True, may_have_started=True, session_status="ERROR")

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)
        monkeypatch.setattr(app_module, "AlertManager", FakeAlertManager)
        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/mapping/run",
            json={
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "truncate_target_table": False,
                "scenario_version": "001",
                "context": "GLOBAL",
                "approved": True,
            },
        )

        body = resp.get_json()
        assert resp.status_code == 502
        assert states[-1][0]["overall_status"] == "RUN_FAILED"
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["alert_type"] == "ODI_RUN_FAILED"
        assert alert["source"] == "MAP_HR_EMPLOYEES_TO_ODS"
        assert alert["severity"] == "CRITICAL"
        assert alert["details"]["batch_id"] == body["batch_id"]
        assert alert["details"]["current_step"] == "run_scenario"
        alert_text = json.dumps(alert, default=str).lower()
        assert "password=secret" not in alert_text
        assert "jdbc:oracle:thin" not in alert_text


class TestMappingGraphWriteEndpoint:
    def test_mapping_generate_graph_uses_graph_after_approval(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_run_mapping_graph(*, user_prompt, params, require_approval, thread_id):
            calls.append({
                "user_prompt": user_prompt,
                "params": params,
                "require_approval": require_approval,
                "thread_id": thread_id,
            })
            return {"status": "planned"}

        monkeypatch.setattr(app_module, "run_mapping_graph", fake_run_mapping_graph)

        resp = app_client.post(
            "/api/mapping/generate-graph",
            json={
                "approved": True,
                "prompt": "build mapping",
                "require_approval": True,
                "thread_id": "thread-2",
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "planned"}
        assert calls == [{
            "user_prompt": "build mapping",
            "params": None,
            "require_approval": True,
            "thread_id": "thread-2",
        }]


class TestMappingUtilityWriteEndpoints:
    def test_mapping_snapshot_creates_named_snapshot(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeMappingSnapshotManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def ensure_table(self):
                calls.append(("ensure_table",))

            def take_snapshot(self, mapping_name, mapping_data, label):
                calls.append(("take_snapshot", mapping_name, mapping_data, label))
                return 42

        monkeypatch.setattr(app_module, "MappingSnapshotManager", FakeMappingSnapshotManager)

        resp = app_client.post(
            "/api/mapping/snapshot",
            json={
                "approved": True,
                "mapping_name": "map_hr_employees_to_ods",
                "mapping_data": [{"target": "EMPLOYEE_ID"}],
                "label": "before update",
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {
            "ok": True,
            "snap_id": 42,
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        }
        assert calls == [
            ("ensure_table",),
            ("take_snapshot", "MAP_HR_EMPLOYEES_TO_ODS", [{"target": "EMPLOYEE_ID"}], "before update"),
        ]

    def test_mapping_snapshot_requires_strict_approval_before_manager(self, app_client, monkeypatch):
        import app as app_module

        def must_not_run():
            raise AssertionError("snapshot manager must not open without approved:true")

        monkeypatch.setattr(app_module, "MappingSnapshotManager", must_not_run)

        for approved in (None, False, 1, "true"):
            body = {"mapping_name": "MAP_STATUS", "mapping_data": []}
            if approved is not None:
                body["approved"] = approved
            resp = app_client.post("/api/mapping/snapshot", json=body)
            assert resp.status_code == 400
            assert "approved:true" in resp.get_json()["error"]

    def test_mapping_analyze_prompt_calls_generator(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeMappingGenerator:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def analyze_prompt(self, prompt):
                calls.append(prompt)
                return {"ok": True, "mapping_name": "MAP_TEST"}

        monkeypatch.setattr(app_module, "MappingGenerator", FakeMappingGenerator)

        resp = app_client.post("/api/mapping/analyze-prompt", json={"prompt": "build mapping"})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "mapping_name": "MAP_TEST"}
        assert calls == ["build mapping"]

    def test_mapping_target_preflight_accepts_flat_params(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeMappingGenerator:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def target_table_preflight(self, params):
                calls.append(params)
                return {"status": "ready", "target": params["tgt_table"]}

        monkeypatch.setattr(app_module, "MappingGenerator", FakeMappingGenerator)

        resp = app_client.post(
            "/api/mapping/target-preflight",
            json={"src_table": "EMPLOYEES", "tgt_table": "ODS_HR_EMPLOYEES"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ready", "target": "ODS_HR_EMPLOYEES"}
        assert calls == [{"src_table": "EMPLOYEES", "tgt_table": "ODS_HR_EMPLOYEES"}]

    def test_mapping_generate_uses_graph_path_after_approval(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_run_mapping_graph(*, user_prompt, params, require_approval, thread_id):
            calls.append({
                "user_prompt": user_prompt,
                "params": params,
                "require_approval": require_approval,
                "thread_id": thread_id,
            })
            return {"status": "planned"}

        monkeypatch.setattr(app_module, "run_mapping_graph", fake_run_mapping_graph)

        resp = app_client.post(
            "/api/mapping/generate",
            json={
                "approved": True,
                "params": {"src_table": "EMPLOYEES", "tgt_table": "ODS_HR_EMPLOYEES"},
                "require_approval": True,
                "thread_id": "thread-1",
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "planned", "engine": "graph"}
        assert calls == [{
            "user_prompt": None,
            "params": {"src_table": "EMPLOYEES", "tgt_table": "ODS_HR_EMPLOYEES"},
            "require_approval": True,
            "thread_id": "thread-1",
        }]

    def test_validate_expression_uses_odi_orchestrator(self, app_client, monkeypatch):
        import odi_orchestrator

        calls = []

        class FakeOdiOrchestrator:
            def validate_expression(self, target_table, target_owner, expressions):
                calls.append((target_table, target_owner, expressions))
                return {"valid": True}

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/mapping/validate-expression",
            json={
                "target_table": "ods_hr_employees",
                "target_owner": "dwh",
                "expressions": {"EMPLOYEE_ID": "EMPLOYEES.EMPLOYEE_ID"},
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"valid": True}
        assert calls == [("ODS_HR_EMPLOYEES", "DWH", {"EMPLOYEE_ID": "EMPLOYEES.EMPLOYEE_ID"})]


class TestMappingRunsEndpoint:
    """GET /api/mapping/runs read-only history contract tests."""

    def test_mapping_runs_returns_recent_runs_from_state_manager(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def list_recent_runs(self, limit=50, status=None):
                calls.append({"limit": limit, "status": status})
                return [
                    {
                        "batch_id": "RUN-001",
                        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                        "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN",
                        "scenario_version": "001",
                        "overall_status": "RUN_FAILED",
                        "current_step": "run_scenario",
                        "truncate_target_table": True,
                        "started_at": "2026-06-18T19:44:00",
                        "updated_at": "2026-06-18T19:44:05",
                        "duration_seconds": 5,
                        "error": "redacted short error",
                    }
                ]

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)

        resp = app_client.get("/api/mapping/runs?limit=5&status=run_failed")

        body = resp.get_json()
        assert resp.status_code == 200
        assert calls == [{"limit": 5, "status": "RUN_FAILED"}]
        assert body["count"] == 1
        assert body["runs"][0]["batch_id"] == "RUN-001"
        assert body["runs"][0]["overall_status"] == "RUN_FAILED"

    def test_mapping_runs_bounds_limit(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def list_recent_runs(self, limit=50, status=None):
                calls.append(limit)
                return []

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)

        resp = app_client.get("/api/mapping/runs?limit=10000")

        assert resp.status_code == 200
        assert calls == [100]


class TestMappingStateEndpoint:
    """GET /api/mapping/state/<mapping_name> read-only state contract tests."""

    def test_mapping_state_returns_persisted_state_from_state_manager(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_by_mapping(self, mapping_name):
                calls.append(mapping_name)
                return {
                    "mapping_name": mapping_name,
                    "overall_status": "COMPLETED",
                    "current_step": "done",
                }

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)

        resp = app_client.get("/api/mapping/state/MAP_HR_EMPLOYEES_TO_ODS")

        body = resp.get_json()
        assert resp.status_code == 200
        assert calls == ["MAP_HR_EMPLOYEES_TO_ODS"]
        assert body == {
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "state": {
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "overall_status": "COMPLETED",
                "current_step": "done",
            },
        }

    def test_mapping_state_returns_404_when_state_missing(self, app_client, monkeypatch):
        import app as app_module

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_by_mapping(self, mapping_name):
                return None

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)

        resp = app_client.get("/api/mapping/state/MAP_UNKNOWN")

        assert resp.status_code == 404
        assert resp.get_json() == {"mapping_name": "MAP_UNKNOWN", "state": None}


class TestMappingLogEndpoint:
    """GET /api/mapping-log/<batch_id> read-only rollback-log contract tests."""

    def test_mapping_log_returns_batch_log_from_rollback_manager(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeRollbackManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_batch_log(self, batch_id):
                calls.append(batch_id)
                return {
                    "batch_id": batch_id,
                    "steps": [
                        {"step": "capture_metadata", "status": "ok"},
                    ],
                }

        monkeypatch.setattr(app_module, "RollbackManager", FakeRollbackManager)

        resp = app_client.get("/api/mapping-log/BATCH-001")

        assert resp.status_code == 200
        assert calls == ["BATCH-001"]
        assert resp.get_json() == {
            "batch_id": "BATCH-001",
            "steps": [
                {"step": "capture_metadata", "status": "ok"},
            ],
        }

    def test_mapping_log_redacts_historical_error_message(self, app_client, monkeypatch):
        import app as app_module

        class FakeRollbackManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_batch_log(self, _batch_id):
                return [{"STATUS": "FAILED", "ERROR_MESSAGE": "internal credential detail must not leak"}]

        monkeypatch.setattr(app_module, "RollbackManager", FakeRollbackManager)

        resp = app_client.get("/api/mapping-log/BATCH-001")

        assert resp.status_code == 200
        assert resp.get_json()[0]["ERROR_MESSAGE"] == "mapping log read failed."
        assert "internal credential detail" not in resp.get_data(as_text=True)


class TestMappingGraphInfoEndpoint:
    """GET /api/mapping/graph-info read-only graph metadata contract tests."""

    def test_mapping_graph_info_returns_filtered_graph_metadata(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeEdge:
            def __init__(self, source, target):
                self.source = source
                self.target = target

        class FakeCompiledGraph:
            nodes = {
                "__start__": object(),
                "analyze": object(),
                "generate": object(),
                "__end__": object(),
            }
            edges = [
                FakeEdge("__start__", "analyze"),
                FakeEdge("analyze", "generate"),
                FakeEdge("generate", "__end__"),
            ]

            def draw_mermaid(self):
                return "graph TD; analyze-->generate"

        class FakeGraph:
            def get_graph(self):
                return FakeCompiledGraph()

        def fake_get_graph(**kwargs):
            calls.append(kwargs)
            return FakeGraph()

        monkeypatch.setattr(app_module, "get_graph", fake_get_graph)

        resp = app_client.get("/api/mapping/graph-info")

        body = resp.get_json()
        assert resp.status_code == 200
        assert calls == [{"use_memory_checkpoint": False}]
        assert body["nodes"] == ["analyze", "generate"]
        assert body["edges"] == [{"source": "analyze", "target": "generate"}]
        assert body["node_count"] == 2
        assert body["edge_count"] == 1
        assert body["mermaid"] == "graph TD; analyze-->generate"
        assert body["orchestrator"] == "LangGraph"
        assert body["checkpoint"] == "MemorySaver (in-memory)"


class TestMappingUpdateEndpoint:
    def test_mapping_update_accepts_truncate_target_table_metadata_only(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeMappingGenerator:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def update_mapping(self, mapping_name, changes):
                calls.append((mapping_name, changes))
                return {"ok": True, "message": "KM option updated"}

        monkeypatch.setattr(app_module, "MappingGenerator", FakeMappingGenerator)

        resp = app_client.post(
            "/api/mapping/update",
            json={
                "mapping_name": "map_hr_employees_to_ods",
                "approved": True,
                "changes": {
                    "update_km": True,
                    "truncate_target_table": True,
                    "tgt_owner": "DWH",
                },
            },
        )

        assert resp.status_code == 200
        assert calls == [
            (
                "MAP_HR_EMPLOYEES_TO_ODS",
                {"update_km": True, "truncate_target_table": True, "tgt_owner": "DWH"},
            )
        ]


class TestMappingPersistentWriteEndpoints:
    def test_mapping_delete_calls_generator_after_approval(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeMappingGenerator:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def delete_mapping(self, mapping_name, tgt_owner):
                calls.append((mapping_name, tgt_owner))
                return {"ok": True, "deleted": mapping_name}

        monkeypatch.setattr(app_module, "MappingGenerator", FakeMappingGenerator)

        resp = app_client.post(
            "/api/mapping/delete",
            json={
                "mapping_name": "map_hr_employees_to_ods",
                "tgt_owner": "DWH",
                "approved": True,
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "deleted": "MAP_HR_EMPLOYEES_TO_ODS"}
        assert calls == [("MAP_HR_EMPLOYEES_TO_ODS", "DWH")]

    def test_rollback_calls_manager_with_batch_id(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeRollbackManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def rollback_batch(self, batch_id):
                calls.append(batch_id)
                return {"ok": True, "batch_id": batch_id}

        monkeypatch.setattr(app_module, "RollbackManager", FakeRollbackManager)

        resp = app_client.post("/api/rollback/RUN-001", json={"approved": True})

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "batch_id": "RUN-001"}
        assert calls == ["RUN-001"]

    def test_rollback_requires_approval_before_manager_construction(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "RollbackManager",
            lambda: pytest.fail("rollback manager must not be constructed without approval"),
        )

        resp = app_client.post("/api/rollback/RUN-001", json={})

        assert resp.status_code == 400
        assert "approved" in resp.get_json()["error"].lower()

    def test_rollback_rejects_invalid_batch_id_before_manager_construction(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "RollbackManager",
            lambda: pytest.fail("rollback manager must not be constructed for invalid batch id"),
        )

        resp = app_client.post("/api/rollback/RUN;DROP", json={"approved": True})

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Invalid batch_id."

    def test_rollback_redacts_http_200_manager_errors(self, app_client, monkeypatch):
        import app as app_module

        class FakeRollbackManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def rollback_batch(self, _batch_id):
                return {
                    "status": "FAILED",
                    "errors": [{"object": "DWH.T1", "error": "internal credential detail must not leak"}],
                }

        monkeypatch.setattr(app_module, "RollbackManager", FakeRollbackManager)

        resp = app_client.post("/api/rollback/RUN-001", json={"approved": True})
        body = resp.get_json()

        assert resp.status_code == 200
        assert body["errors"][0]["error"] == "mapping rollback failed."
        assert "internal credential detail" not in resp.get_data(as_text=True)


class TestAskTruncateIntent:
    @pytest.mark.parametrize(
        "message",
        [
            "MAP_HR_EMPLOYEES_TO_ODS icin truncate ekle",  # intentional Turkish input
            "MAP_HR_EMPLOYEES_TO_ODS target truncate",
        ],
    )
    def test_truncate_intent_returns_confirmation_request_without_llm_or_run(self, app_client, monkeypatch, message):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "ask_ai",
            lambda *_args, **_kwargs: pytest.fail("truncate intent must not fall through to LLM"),
        )

        resp = app_client.post(
            "/api/ask",
            json={
                "message": message,
                "history": [],
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["requires_confirmation"] is True
        assert body["action_request"]["type"] == "mapping_km_option_update"
        assert body["action_request"]["mapping_name"] == "MAP_HR_EMPLOYEES_TO_ODS"
        assert body["action_request"]["proposed_changes"] == {"truncate_target_table": True}
        assert body["action_request"]["will_run_scenario"] is False

    def test_truncate_intent_without_mapping_asks_for_mapping_name(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "ask_ai",
            lambda *_args, **_kwargs: pytest.fail("truncate intent must be deterministic"),
        )

        resp = app_client.post(
            "/api/ask",
            json={"message": "append yapma target truncate olsun", "history": []},
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["requires_mapping"] is True
        assert "mapping" in body["aciklama"].lower()

    def test_truncate_intent_confirmation_text_is_clean_english(self):
        import ai_helpers

        body = ai_helpers._truncate_mapping_intent_answer(
            "MAP_HR_EMPLOYEES_TO_ODS truncate ekle"
        )

        assert body["aciklama"].startswith(
            "`MAP_HR_EMPLOYEES_TO_ODS` proposes `TRUNCATE_TARGET_TABLE=true`."
        )
        assert "Ã" not in body["aciklama"]
        assert "Â" not in body["aciklama"]

    def test_modeled_tables_inventory_text_is_clean_english(self, monkeypatch):
        import ai_helpers

        monkeypatch.setattr(ai_helpers, "odi_get_mapping_lineage", lambda limit=100: "[]")

        body = ai_helpers._modeled_tables_answer()

        assert body["aciklama"] == "Tables modeled in the current ODI mapping inventory are listed below."
        assert "Ã" not in body["aciklama"]
        assert "Â" not in body["aciklama"]


class TestAiWriteEndpoints:
    def test_ai_orchestrate_boolean_flags_are_strict(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_orchestrate(prompt, *, dry_run, allow_cloud_review, job_id):
            calls.append((prompt, dry_run, allow_cloud_review, job_id))
            return {"status": "planned"}

        monkeypatch.setattr(app_module, "orchestrate", fake_orchestrate)

        resp = app_client.post(
            "/api/ai/orchestrate",
            json={
                "prompt": "plan metadata flow",
                "dry_run": "false",
                "allow_cloud_review": "false",
            },
        )

        assert resp.status_code == 200
        assert calls == [("plan metadata flow", True, False, None)]

    def test_config_llm_rejects_unknown_provider_without_setting_env(self, app_client, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)

        resp = app_client.post("/api/config/llm", json={"provider": "not-a-provider"})

        assert resp.status_code == 400
        assert "LLM_PROVIDER" not in os.environ

    def test_local_analyst_summarize_requires_content_before_calling_llm(self, app_client, monkeypatch):
        import sys
        import types

        fake_ai_agent = types.SimpleNamespace(
            call_llm=lambda *_args, **_kwargs: pytest.fail("LLM must not run without content")
        )
        monkeypatch.setitem(sys.modules, "ai_agent", fake_ai_agent)

        resp = app_client.post("/api/local-analyst/summarize", json={"title": "No content"})

        assert resp.status_code == 400
        assert resp.get_json()["error"]

    def test_ai_orchestrate_requires_prompt_before_orchestration(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "orchestrate",
            lambda *_args, **_kwargs: pytest.fail("orchestration must not run without a prompt"),
        )

        resp = app_client.post("/api/ai/orchestrate", json={"prompt": ""})

        assert resp.status_code == 400
        assert resp.get_json()["error"]

    def test_rag_knowledge_get_uses_fake_rag_store_without_raw_backend(self, app_client, monkeypatch):
        import sys
        import types

        fake_rag_manager = types.SimpleNamespace(
            _load_db=lambda: {
                "docs": [
                    {"id": "doc-1", "text": "metadata-only", "metadata": {"kind": "test"}}
                ]
            },
            add_document=lambda *_args, **_kwargs: pytest.fail("GET must not add documents"),
            delete_document=lambda *_args, **_kwargs: pytest.fail("GET must not delete documents"),
        )
        monkeypatch.setitem(sys.modules, "rag_manager", fake_rag_manager)

        resp = app_client.get("/api/rag_knowledge")

        assert resp.status_code == 200
        assert resp.get_json() == {
            "docs": [{"id": "doc-1", "text": "metadata-only", "metadata": {"kind": "test"}}]
        }

    @pytest.mark.parametrize("method", ["post", "delete"])
    def test_rag_knowledge_writes_require_approval_before_store_access(
        self, app_client, monkeypatch, method
    ):
        import sys
        import types

        fake_rag_manager = types.SimpleNamespace(
            _load_db=lambda: pytest.fail("write approval must be checked before store access"),
            add_document=lambda *_args, **_kwargs: pytest.fail(
                "write approval must be checked before adding a document"
            ),
            delete_document=lambda *_args, **_kwargs: pytest.fail(
                "write approval must be checked before deleting a document"
            ),
        )
        monkeypatch.setitem(sys.modules, "rag_manager", fake_rag_manager)

        payload = {"collection": "docs", "id": "doc-1", "text": "metadata-only"}
        resp = getattr(app_client, method)("/api/rag_knowledge", json=payload)

        assert resp.status_code == 400
        assert resp.get_json() == {
            "error": "approved:true is required for RAG knowledge write."
        }

    def test_rag_knowledge_write_uses_store_after_approval(self, app_client, monkeypatch):
        import sys
        import types

        calls = []
        fake_rag_manager = types.SimpleNamespace(
            _load_db=lambda: {},
            add_document=lambda collection, text, metadata, doc_id: calls.append(
                (collection, text, metadata, doc_id)
            )
            or "doc-1",
            delete_document=lambda *_args, **_kwargs: False,
        )
        monkeypatch.setitem(sys.modules, "rag_manager", fake_rag_manager)

        resp = app_client.post(
            "/api/rag_knowledge",
            json={
                "approved": True,
                "collection": "docs",
                "text": "metadata-only",
                "metadata": {"kind": "test"},
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, "id": "doc-1"}
        assert calls == [("docs", "metadata-only", {"kind": "test"}, None)]


class TestSecondaryWriteErrorRedaction:
    def test_secondary_write_route_sources_do_not_return_raw_exception_500s(self):
        route_files = [
            "routes/quality_write.py",
            "routes/mapping_write.py",
            "routes/misc_write.py",
            "routes/ai_write.py",
            "routes/schema_write.py",
            "routes/environment.py",
        ]
        unsafe_patterns = [
            'return jsonify({"error": str(exc)}), 500',
            'return jsonify({"status": "error", "error": str(exc)}), 500',
            'Validation failed: {str(exc)}',
            "error_msg = str(exc)",
            "str(retry_exc)",
        ]

        for rel_path in route_files:
            content = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
            for pattern in unsafe_patterns:
                assert pattern not in content, f"{rel_path} still returns raw exception text"

    def test_ai_ask_validation_request_failure_redacts_user_visible_error(self, app_client, monkeypatch):
        import sys
        import types
        import app as app_module

        sensitive_detail = "jdbc:oracle:thin:@secret-host password=top-secret"
        fake_validation = types.SimpleNamespace(
            validate_column_accuracy_by_pk=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(sensitive_detail))
        )
        monkeypatch.setitem(sys.modules, "data_validation_mcp", fake_validation)
        monkeypatch.setattr(
            app_module,
            "ask_ai",
            lambda *_args, **_kwargs: (
                {
                    "validation_request": {
                        "src_table": "SRC_TABLE",
                        "tgt_table": "TGT_TABLE",
                        "pk_col": "ID",
                        "col_to_check": "STATUS",
                    }
                },
                "metadata-only raw response",
            ),
        )

        resp = app_client.post("/api/ask", json={"message": "validate columns"})

        assert resp.status_code == 200
        body = resp.get_json()
        response_text = json.dumps(body)
        assert body["aciklama"] == "Validation failed. See server logs for details."
        assert "jdbc:oracle:thin" not in response_text
        assert "password=top-secret" not in response_text

    def test_ai_ask_sql_preview_failure_redacts_retry_error(self, app_client, monkeypatch):
        import app as app_module

        sensitive_detail = "ORA-01017 invalid password=top-secret jdbc:oracle:thin:@secret-host/service"
        ask_calls = []

        def fake_ask_ai(prompt, *_args, **_kwargs):
            ask_calls.append(prompt)
            if len(ask_calls) == 1:
                return (
                    {"queries": [{"action": "select", "label": "Preview", "sql": "select * from sensitive_view"}]},
                    "metadata-only raw response",
                )
            return ({"queries": []}, "retry raw response")

        monkeypatch.setattr(app_module, "ask_ai", fake_ask_ai)
        monkeypatch.setattr(app_module, "_is_llm_status_question", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(app_module, "_is_truncate_mapping_intent", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(app_module, "_is_remembered_unmapped_flow_mapping_request", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(app_module, "_is_unmapped_odi_flow_question", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(app_module, "_is_modeled_tables_question", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(app_module, "_is_table_data_request", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(
            app_module,
            "execute_select",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(sensitive_detail)),
        )

        resp = app_client.post("/api/ask", json={"message": "show data"})

        assert resp.status_code == 200
        body = resp.get_json()
        response_text = json.dumps(body)
        assert body["queries"][0]["error"] == "SQL preview failed. See server logs for details."
        assert "jdbc:oracle:thin" not in response_text
        assert "password=top-secret" not in response_text
        assert "ORA-01017" not in response_text
        assert "password=top-secret" not in ask_calls[-1]
        assert "jdbc:oracle:thin" not in ask_calls[-1]

    def test_project_hub_error_is_redacted(self, app_client, monkeypatch, tmp_path):
        import app as app_module

        sensitive_detail = "jdbc:oracle:thin:@secret-host password=top-secret"
        (tmp_path / "project_hub.json").write_text(
            json.dumps({"architecture": [{"id": "oracle", "status": "Unknown"}]}),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            app_module,
            "test_connection",
            lambda: (_ for _ in ()).throw(RuntimeError(sensitive_detail)),
        )

        resp = app_client.get("/api/project_hub")

        assert resp.status_code == 500
        body = resp.get_json()
        response_text = json.dumps(body)
        assert body == {"error": "Project hub status check failed.", "error_type": "RuntimeError"}
        assert "jdbc:oracle:thin" not in response_text
        assert "password=top-secret" not in response_text

    def test_mapping_run_operator_failure_response_is_redacted(self, app_client, monkeypatch):
        import app as app_module
        import odi_orchestrator

        states = []
        sensitive_detail = "FATAL ERROR: password=top-secret jdbc:oracle:thin:@secret-host/service"

        class FakeStateManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def upsert_state(self, state, thread_id=None):
                states.append((state, thread_id))

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def update_mapping(self, **_kwargs):
                return sensitive_detail

            def generate_scenario_metadata(self, *_args, **_kwargs):
                pytest.fail("generate_scenario must not run after failed option update")

        monkeypatch.setattr(app_module, "MappingStateManager", FakeStateManager)
        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/mapping/run",
            json={
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "truncate_target_table": True,
                "scenario_version": "001",
                "approved": True,
            },
        )

        body = resp.get_json()
        response_text = json.dumps(body)
        assert resp.status_code == 500
        assert body["error"] == "Scenario option update failed."
        assert body["batch_id"].startswith("RUN-")
        assert "jdbc:oracle:thin" not in response_text
        assert "password=top-secret" not in response_text
        state_text = json.dumps(states[-1][0], default=str)
        assert "jdbc:oracle:thin" not in state_text
        assert "password=top-secret" not in state_text

    def test_legacy_airflow_mapping_operator_failure_response_is_redacted(self, app_client, monkeypatch):
        import odi_orchestrator

        sensitive_detail = "Hata: password=top-secret jdbc:oracle:thin:@secret-host/service"

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def generate_scenario_metadata(self, *_args, **_kwargs):
                return sensitive_detail

            def run_scenario_remote(self, *_args, **_kwargs):
                pytest.fail("run_scenario must not run after failed generation")

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/airflow/run-mapping",
            json={
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "scenario_version": "001",
                "approved": True,
            },
        )

        body = resp.get_json()
        response_text = json.dumps(body)
        assert resp.status_code == 500
        assert body == {"status": "error", "error": "Scenario generation failed."}
        assert "jdbc:oracle:thin" not in response_text
        assert "password=top-secret" not in response_text

    def test_representative_secondary_write_errors_are_redacted(self, app_client, monkeypatch):
        import app as app_module

        sensitive_detail = "jdbc:oracle:thin:@secret-host password=top-secret"

        class BrokenDataQualityManager:
            def __enter__(self):
                raise RuntimeError(sensitive_detail)

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        class BrokenLineageManager:
            def __enter__(self):
                raise RuntimeError(sensitive_detail)

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        monkeypatch.setattr(app_module, "DataQualityManager", BrokenDataQualityManager)
        monkeypatch.setattr(app_module, "LineageManager", BrokenLineageManager)
        monkeypatch.setattr(
            app_module,
            "run_mapping_graph",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(sensitive_detail)),
        )
        monkeypatch.setattr(
            app_module,
            "orchestrate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(sensitive_detail)),
        )

        cases = [
            ("/api/quality/validate", {"owner": "DWH", "table": "ODS_TEST"}),
            ("/api/mapping/generate-graph", {"approved": True, "prompt": "build mapping"}),
            ("/api/lineage/refresh", {"approved": True}),
            ("/api/ai/orchestrate", {"prompt": "plan this"}),
        ]

        for url, payload in cases:
            resp = app_client.post(url, json=payload)

            assert resp.status_code == 500
            body = resp.get_json()
            assert body["error_type"] == "RuntimeError"
            response_text = json.dumps(body)
            assert "jdbc:oracle:thin" not in response_text
            assert "password=top-secret" not in response_text

    def test_schema_write_validation_text_is_english_not_mojibake(self, app_client):
        invalid_owner_resp = app_client.post("/api/schema-changes/detect", json={"owner": "bad"})
        missing_table_resp = app_client.post("/api/watermarks/delta", json={"owner": "DWH"})
        invalid_preview_format_resp = app_client.get("/api/preview/not_a_qualified_table")
        invalid_preview_owner_resp = app_client.get("/api/preview/BAD.ODS_STATUS")
        invalid_source_preview_format_resp = app_client.get("/api/source/preview/not_a_qualified_table")

        assert invalid_owner_resp.status_code == 400
        assert missing_table_resp.status_code == 400
        assert invalid_preview_format_resp.status_code == 400
        assert invalid_preview_owner_resp.status_code == 400
        assert invalid_source_preview_format_resp.status_code == 400
        assert invalid_owner_resp.get_json()["error"] == "Invalid owner: BAD"
        assert missing_table_resp.get_json()["error"] == "Table is required."
        assert invalid_preview_format_resp.get_json()["error"] == "Invalid table name format."
        assert invalid_preview_owner_resp.get_json()["error"] == "Invalid owner: BAD"
        assert invalid_source_preview_format_resp.get_json()["error"] == "Invalid table name format."

        route_text = (PROJECT_ROOT / "routes/schema_write.py").read_text(encoding="utf-8")
        schema_read_text = (PROJECT_ROOT / "routes/schema_read.py").read_text(encoding="utf-8")
        for content in (route_text, schema_read_text):
            assert "Ã" not in content
            assert "Â" not in content
            assert "â" not in content


class TestSchemaImpactAndPreflight:
    def test_schema_impact_returns_mapping_blast_radius(self, app_client, monkeypatch):
        import app as app_module

        class FakeLineageManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_impact_analysis(self, table_name, direction="both"):
                assert table_name == "ODS_HR_EMPLOYEES"
                assert direction == "both"
                return [
                    {
                        "direction": "upstream",
                        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                        "related_table": "HR.EMPLOYEES",
                    }
                ]

        monkeypatch.setattr(app_module, "LineageManager", FakeLineageManager)
        monkeypatch.setattr(
            app_module,
            "get_table_schema",
            lambda table_ref: pd.DataFrame([
                {"COLUMN_NAME": "EMPLOYEE_ID", "FULL_DATA_TYPE": "NUMBER(6)", "NULLABLE": "N"},
                {"COLUMN_NAME": "FIRST_NAME", "FULL_DATA_TYPE": "VARCHAR2(20)", "NULLABLE": "Y"},
            ]),
        )

        resp = app_client.get("/api/schema/impact?object=DWH.ODS_HR_EMPLOYEES.FIRST_NAME")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["object"] == {
            "owner": "DWH",
            "table": "ODS_HR_EMPLOYEES",
            "column": "FIRST_NAME",
        }
        assert body["affected_count"] == 1
        assert body["mappings"][0]["mapping_name"] == "MAP_HR_EMPLOYEES_TO_ODS"
        assert body["columns"] == [
            {"column_name": "FIRST_NAME", "data_type": "VARCHAR2(20)", "nullable": "Y"}
        ]

    def test_schema_preflight_blocks_unsafe_varchar_narrowing(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "_get_db_column_definitions",
            lambda owner, table: [
                {
                    "column_name": "FIRST_NAME",
                    "data_type": "VARCHAR2",
                    "data_length": 30,
                    "data_precision": None,
                    "data_scale": None,
                    "nullable": "Y",
                    "full_data_type": "VARCHAR2(30)",
                }
            ],
        )
        monkeypatch.setattr(app_module, "_max_text_length", lambda owner, table, column: 25)
        monkeypatch.setattr(app_module, "_schema_impact_payload", lambda object_ref: {"affected_count": 1, "mappings": []})

        resp = app_client.post(
            "/api/schema/change/preflight",
            json={
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(20)",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["read_only"] is True
        assert body["operation"] == "modify_type"
        assert body["risk"]["classification"] == "blocked_narrowing"
        assert body["allowed_to_apply"] is False
        assert body["requires_approval"] is True

    def test_schema_findings_endpoint_returns_read_only_structural_findings(self, app_client, monkeypatch):
        import app as app_module

        def fake_findings(owner):
            assert owner == "DWH"
            return {
                "owner": owner,
                "finding_count": 1,
                "read_only": True,
                "findings": [
                    {
                        "kind": "column_type_size_mismatch",
                        "table": "ODS_HR_EMPLOYEES",
                        "column": "FIRST_NAME",
                        "db_type": "VARCHAR2(30)",
                        "odi_type": "VARCHAR2(20)",
                        "summary": "DWH.ODS_HR_EMPLOYEES.FIRST_NAME DB VARCHAR2(30) != ODI VARCHAR2(20)",
                    }
                ],
            }

        monkeypatch.setattr(app_module, "build_schema_sync_findings", fake_findings)

        resp = app_client.get("/api/schema/findings?owner=DWH")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["read_only"] is True
        assert body["finding_count"] == 1
        assert body["findings"][0]["kind"] == "column_type_size_mismatch"
        assert "sample" not in str(body).lower()
        assert "value" not in str(body).lower()

    def test_schema_drift_returns_owner_count_and_read_only_flag(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_drift(owner):
            calls.append(owner)
            return [
                {
                    "owner": owner,
                    "table": "ODS_HR_EMPLOYEES",
                    "column": "FIRST_NAME",
                    "db_type": "VARCHAR2(30)",
                    "odi_type": "VARCHAR2(20)",
                }
            ]

        monkeypatch.setattr(app_module, "detect_db_odi_type_drift", fake_drift)

        resp = app_client.get("/api/schema/drift?owner=DWH")

        body = resp.get_json()
        assert resp.status_code == 200
        assert calls == ["DWH"]
        assert body["owner"] == "DWH"
        assert body["drift_count"] == 1
        assert body["read_only"] is True
        assert body["drift"][0]["table"] == "ODS_HR_EMPLOYEES"

    def test_schema_changes_serializes_datetime_like_values(self, app_client, monkeypatch):
        import app as app_module

        class FakeTimestamp:
            def isoformat(self):
                return "2026-07-03T12:00:00"

        calls = []

        def fake_recent_changes(limit=20):
            calls.append(limit)
            return [{"table_name": "ODS_HR_EMPLOYEES", "detected_at": FakeTimestamp()}]

        monkeypatch.setattr(app_module, "get_recent_changes", fake_recent_changes)

        resp = app_client.get("/api/schema-changes")

        assert resp.status_code == 200
        assert calls == [20]
        assert resp.get_json() == {
            "changes": [{"table_name": "ODS_HR_EMPLOYEES", "detected_at": "2026-07-03T12:00:00"}]
        }

    def test_watermarks_lists_and_serializes_datetime_like_values(self, app_client, monkeypatch):
        import app as app_module

        class FakeTimestamp:
            def isoformat(self):
                return "2026-07-03T12:30:00"

        class FakeWatermarkManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def list_all_watermarks(self):
                return [{"table_name": "DWH.ODS_HR_EMPLOYEES", "last_value": FakeTimestamp()}]

        monkeypatch.setattr(app_module, "WatermarkManager", FakeWatermarkManager)

        resp = app_client.get("/api/watermarks")

        assert resp.status_code == 200
        assert resp.get_json() == {
            "watermarks": [{"table_name": "DWH.ODS_HR_EMPLOYEES", "last_value": "2026-07-03T12:30:00"}]
        }

    def test_schema_endpoint_renames_full_data_type(self, app_client, monkeypatch):
        import app as app_module

        def fake_schema(table_name):
            assert table_name == "DWH.ODS_STATUS"
            return pd.DataFrame([
                {"COLUMN_NAME": "STATUS_CODE", "FULL_DATA_TYPE": "VARCHAR2(12)", "NULLABLE": "N"}
            ])

        monkeypatch.setattr(app_module, "get_table_schema", fake_schema)

        resp = app_client.get("/api/schema/DWH.ODS_STATUS")

        assert resp.status_code == 200
        assert resp.get_json() == [
            {"COLUMN_NAME": "STATUS_CODE", "DATA_TYPE": "VARCHAR2(12)", "NULLABLE": "N"}
        ]

    def test_source_schema_endpoint_returns_comments_without_nulls(self, app_client, monkeypatch):
        import app as app_module

        def fake_source_schema(table_name):
            assert table_name == "HR.STATUS_SOURCE"
            return pd.DataFrame([
                {"COLUMN_NAME": "STATUS_CODE", "FULL_DATA_TYPE": "VARCHAR2(12)", "COMMENTS": pd.NA}
            ])

        monkeypatch.setattr(app_module, "get_source_table_schema", fake_source_schema)

        resp = app_client.get("/api/source/schema/HR.STATUS_SOURCE")

        assert resp.status_code == 200
        assert resp.get_json() == [
            {"COLUMN_NAME": "STATUS_CODE", "DATA_TYPE": "VARCHAR2(12)", "NULLABLE": "", "COMMENTS": ""}
        ]

    def test_preview_endpoint_uses_owner_guard_and_preview_limit(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_build_sql(table_name, limit):
            calls.append((table_name, limit))
            return "SELECT STATUS_CODE FROM DWH.ODS_STATUS WHERE ROWNUM <= 3"

        monkeypatch.setattr(app_module, "_build_table_preview_sql", fake_build_sql)
        monkeypatch.setattr(
            app_module,
            "execute_select",
            lambda sql: pd.DataFrame([{"STATUS_CODE": "OK"}]),
        )

        resp = app_client.get("/api/preview/DWH.ODS_STATUS")

        assert resp.status_code == 200
        assert calls == [("DWH.ODS_STATUS", 3)]
        assert resp.get_json() == {"columns": ["STATUS_CODE"], "rows": [{"STATUS_CODE": "OK"}]}

    def test_source_preview_endpoint_uses_source_connector_sample_limit(self, app_client, monkeypatch):
        import source_connector

        class FakeSourceConnector:
            def sample_data(self, table_name, limit):
                assert table_name == "HR.STATUS_SOURCE"
                assert limit == 3
                return pd.DataFrame([{"STATUS_CODE": "OK"}])

        monkeypatch.setattr(source_connector, "get_source_connector", lambda: FakeSourceConnector())

        resp = app_client.get("/api/source/preview/HR.STATUS_SOURCE")

        assert resp.status_code == 200
        assert resp.get_json() == {"columns": ["STATUS_CODE"], "rows": [{"STATUS_CODE": "OK"}]}

    def test_incremental_preflight_endpoint_returns_read_only_report(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_validate(spec_name):
            calls.append(spec_name)
            return "SH_SALES_ODS"

        def fake_preflight(specs):
            assert specs == ["SH_SALES_ODS"]
            return {
                "status": "passed",
                "mode": "dry_run_read_only",
                "raw_rows_included": False,
                "secret_printed": False,
                "results": [{"spec": "SH_SALES_ODS", "will_run_incremental": False}],
            }

        monkeypatch.setattr(app_module, "_validate_incremental_spec_name", fake_validate)
        monkeypatch.setattr(app_module, "run_configured_incremental_strategy_preflight", fake_preflight)

        resp = app_client.get("/api/incremental/preflight?spec_name=SH_SALES_ODS")

        assert resp.status_code == 200
        assert calls == ["SH_SALES_ODS"]
        body = resp.get_json()
        assert body["mode"] == "dry_run_read_only"
        assert body["raw_rows_included"] is False
        assert body["secret_printed"] is False

    def test_schema_findings_export_returns_metadata_only_csv(self, app_client, monkeypatch):
        import app as app_module

        def fake_findings(owner):
            assert owner == "DWH"
            return {
                "owner": owner,
                "finding_count": 1,
                "read_only": True,
                "scope": "db_vs_odi_structural_metadata",
                "findings": [
                    {
                        "kind": "column_type_size_mismatch",
                        "severity": "warning",
                        "owner": "DWH",
                        "table": "ODS_HR_EMPLOYEES",
                        "column": "FIRST_NAME",
                        "db_type": "VARCHAR2(30)",
                        "odi_type": "VARCHAR2(20)",
                        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                        "summary": "DWH.ODS_HR_EMPLOYEES.FIRST_NAME DB VARCHAR2(30) != ODI VARCHAR2(20)",
                        "next_step": "Use Schema Sync preflight.",
                    }
                ],
            }

        monkeypatch.setattr(app_module, "build_schema_sync_findings", fake_findings)

        resp = app_client.get("/api/schema/findings/export?owner=DWH")

        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert "schema_findings_DWH_" in resp.headers["Content-Disposition"]
        text = resp.get_data(as_text=True)
        assert "kind,severity,owner,table,column,db_type,odi_type" in text
        assert "column_type_size_mismatch,warning,DWH,ODS_HR_EMPLOYEES,FIRST_NAME" in text
        assert "MAP_HR_EMPLOYEES_TO_ODS" in text
        assert "sample" not in text.lower()
        assert "password" not in text.lower()
        assert "jdbc:" not in text.lower()

    def test_source_target_findings_endpoint_returns_read_only_metadata(self, app_client, monkeypatch):
        import app as app_module

        def fake_report(source_schema, target_owner):
            assert source_schema == "HR"
            assert target_owner == "DWH"
            return {
                "source_schema": "HR",
                "target_owner": "DWH",
                "finding_count": 1,
                "read_only": True,
                "mapping_link_aware": False,
                "findings": [
                    {
                        "kind": "source_target_type_size_mismatch",
                        "source_table": "HR.EMPLOYEES",
                        "target_table": "DWH.ODS_HR_EMPLOYEES",
                        "column": "FIRST_NAME",
                        "source_type": "VARCHAR2(20)",
                        "target_type": "VARCHAR2(30)",
                        "summary": "HR.EMPLOYEES.FIRST_NAME source VARCHAR2(20) != target VARCHAR2(30)",
                    }
                ],
            }

        monkeypatch.setattr(app_module, "build_source_target_metadata_findings", fake_report)

        resp = app_client.get("/api/schema/source-target/findings?source_schema=HR&target_owner=DWH")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["read_only"] is True
        assert body["mapping_link_aware"] is False
        assert body["finding_count"] == 1
        assert body["findings"][0]["kind"] == "source_target_type_size_mismatch"
        assert "sample" not in str(body).lower()
        assert "password" not in str(body).lower()
        assert "jdbc:" not in str(body).lower()

    def test_pk_parity_endpoint_returns_count_only_read_only_report(self, app_client, monkeypatch):
        import app as app_module

        def fake_report(source_schema, target_owner, max_keys=100000):
            assert source_schema == "HR"
            assert target_owner == "DWH"
            assert max_keys == 10
            return {
                "source_schema": source_schema,
                "target_owner": target_owner,
                "status": "attention_required",
                "read_only": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "results": [
                    {
                        "source_table": "HR.EMPLOYEES",
                        "target_table": "DWH.ODS_HR_EMPLOYEES",
                        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                        "status": "failed",
                        "source_row_count": 2,
                        "target_row_count": 2,
                        "missing_key_count": 1,
                        "extra_key_count": 1,
                    }
                ],
            }

        monkeypatch.setattr(app_module, "build_pk_parity_report", fake_report, raising=False)

        resp = app_client.get("/api/schema/pk-parity?source_schema=HR&target_owner=DWH&max_keys=10")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["read_only"] is True
        assert body["raw_rows_included"] is False
        assert body["pk_values_included"] is False
        assert body["results"][0]["missing_key_count"] == 1
        assert "SRC_PK_ALPHA" not in str(body)
        assert "password" not in str(body).lower()

    def test_pk_parity_endpoint_clamps_requested_max_keys(self, app_client, monkeypatch):
        import app as app_module

        expected_ceiling = 100000

        def fake_report(source_schema, target_owner, max_keys=100000):
            assert source_schema == "HR"
            assert target_owner == "DWH"
            assert max_keys == expected_ceiling
            return {
                "source_schema": source_schema,
                "target_owner": target_owner,
                "status": "passed",
                "read_only": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "max_keys": max_keys,
                "results": [],
            }

        monkeypatch.setattr(app_module, "build_pk_parity_report", fake_report, raising=False)

        resp = app_client.get(
            "/api/schema/pk-parity?source_schema=HR&target_owner=DWH&max_keys=999999999"
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["max_keys"] == expected_ceiling
        assert body["read_only"] is True
        assert body["raw_rows_included"] is False
        assert body["pk_values_included"] is False

    def test_schema_preflight_allows_safe_varchar_widening_as_read_only(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "_get_db_column_definitions",
            lambda owner, table: [
                {
                    "column_name": "FIRST_NAME",
                    "data_type": "VARCHAR2",
                    "data_length": 20,
                    "data_precision": None,
                    "data_scale": None,
                    "nullable": "Y",
                    "full_data_type": "VARCHAR2(20)",
                }
            ],
        )
        monkeypatch.setattr(app_module, "_schema_impact_payload", lambda object_ref: {"affected_count": 1, "mappings": []})

        resp = app_client.post(
            "/api/schema/change/preflight",
            json={
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(30)",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["read_only"] is True
        assert body["risk"]["classification"] == "safe_widening"
        assert body["allowed_to_apply"] is True
        assert body["requires_approval"] is True

    def test_schema_preflight_rename_column_noops_when_already_renamed(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "_get_db_column_definitions",
            lambda owner, table: [
                {
                    "column_name": "GIVEN_NAME",
                    "data_type": "VARCHAR2",
                    "data_length": 30,
                    "data_precision": None,
                    "data_scale": None,
                    "nullable": "Y",
                    "full_data_type": "VARCHAR2(30)",
                }
            ],
        )
        monkeypatch.setattr(app_module, "_schema_impact_payload", lambda object_ref: {"affected_count": 1, "mappings": []})

        resp = app_client.post(
            "/api/schema/change/preflight",
            json={
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "rename_column",
                "new_name": "GIVEN_NAME",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["risk"]["classification"] == "no_op"
        assert body["allowed_to_apply"] is True
        assert body["next_step"].startswith("No change is needed")

    def test_schema_preflight_rename_table_noops_when_already_renamed(self, app_client, monkeypatch):
        import app as app_module

        def fake_columns(owner, table):
            if table == "ODS_HR_EMPLOYEES_V2":
                return [{"column_name": "EMPLOYEE_ID", "full_data_type": "NUMBER"}]
            return []

        monkeypatch.setattr(app_module, "_get_db_column_definitions", fake_columns)
        monkeypatch.setattr(app_module, "_schema_impact_payload", lambda object_ref: {"affected_count": 1, "mappings": []})

        resp = app_client.post(
            "/api/schema/change/preflight",
            json={
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "op": "rename_table",
                "new_name": "ODS_HR_EMPLOYEES_V2",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["risk"]["classification"] == "no_op"
        assert body["allowed_to_apply"] is True
        assert body["object"]["table"] == "ODS_HR_EMPLOYEES"

    def test_schema_apply_db_requires_approved_true(self, app_client):
        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(30)",
            },
        )

        assert resp.status_code == 400
        assert "approved" in resp.get_json()["error"].lower()

    def test_schema_apply_db_rejects_blocked_narrowing(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "_schema_change_preflight_payload",
            lambda body: {
                "read_only": True,
                "operation": "modify_type",
                "object": {"owner": "DWH", "table": "ODS_HR_EMPLOYEES", "column": "FIRST_NAME"},
                "requested": {"new_type": "VARCHAR2(20)", "new_name": None},
                "risk": {"classification": "blocked_narrowing"},
                "allowed_to_apply": False,
                "requires_approval": True,
            },
        )
        monkeypatch.setattr(
            app_module,
            "execute_dml",
            lambda *_args, **_kwargs: pytest.fail("blocked narrowing must not execute DDL"),
        )

        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={
                "approved": True,
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(20)",
            },
        )

        assert resp.status_code == 400
        assert resp.get_json()["preflight"]["risk"]["classification"] == "blocked_narrowing"

    def test_schema_apply_db_redacts_unhandled_exception_message(self, app_client, monkeypatch):
        import app as app_module

        def boom(_body):
            raise RuntimeError("internal credential detail must not leak")

        monkeypatch.setattr(app_module, "_schema_change_apply_db_payload", boom)

        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={"approved": True, "object_ref": "DWH.ODS_HR_EMPLOYEES.FIRST_NAME"},
        )

        assert resp.status_code == 500
        body = json.dumps(resp.get_json())
        assert "RuntimeError" in body
        assert "schema change apply failed" in body.lower()
        assert "internal credential detail" not in body

    def test_schema_apply_db_safe_widening_executes_validated_modify(self, app_client, monkeypatch):
        import app as app_module

        executed = []

        monkeypatch.setattr(
            app_module,
            "_schema_change_preflight_payload",
            lambda body: {
                "read_only": True,
                "operation": "modify_type",
                "object": {"owner": "DWH", "table": "ODS_HR_EMPLOYEES", "column": "FIRST_NAME"},
                "current_column": {"full_data_type": "VARCHAR2(20)"},
                "requested": {"new_type": "VARCHAR2(30)", "new_name": None},
                "risk": {"classification": "safe_widening"},
                "allowed_to_apply": True,
                "requires_approval": True,
            },
        )
        monkeypatch.setattr(app_module, "execute_dml", lambda sql: executed.append(sql) or "OK")
        monkeypatch.setattr(
            app_module,
            "_get_db_column_definitions",
            lambda owner, table: [
                {
                    "column_name": "FIRST_NAME",
                    "data_type": "VARCHAR2",
                    "data_length": 30,
                    "data_precision": None,
                    "data_scale": None,
                    "nullable": "Y",
                    "full_data_type": "VARCHAR2(30)",
                }
            ],
        )

        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={
                "approved": True,
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(30)",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] is True
        assert body["operation"] == "modify_type"
        assert executed == ['ALTER TABLE "DWH"."ODS_HR_EMPLOYEES" MODIFY ("FIRST_NAME" VARCHAR2(30))']
        assert body["after"]["full_data_type"] == "VARCHAR2(30)"

    def test_schema_apply_db_modify_type_noops_when_already_current(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "_schema_change_preflight_payload",
            lambda body: {
                "read_only": True,
                "operation": "modify_type",
                "object": {"owner": "DWH", "table": "ODS_HR_EMPLOYEES", "column": "FIRST_NAME"},
                "current_column": {"full_data_type": "VARCHAR2(30)"},
                "requested": {"new_type": "VARCHAR2(30)", "new_name": None},
                "risk": {"classification": "no_op"},
                "allowed_to_apply": True,
                "requires_approval": True,
            },
        )
        monkeypatch.setattr(
            app_module,
            "execute_dml",
            lambda *_args, **_kwargs: pytest.fail("no-op must not execute DDL"),
        )

        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={
                "approved": True,
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(30)",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] is False
        assert body["status"] == "NO_OP"

    def test_schema_apply_db_rename_noops_when_preflight_is_noop(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "_schema_change_preflight_payload",
            lambda body: {
                "read_only": True,
                "operation": "rename_column",
                "object": {"owner": "DWH", "table": "ODS_HR_EMPLOYEES", "column": "FIRST_NAME"},
                "requested": {"new_type": None, "new_name": "GIVEN_NAME"},
                "risk": {"classification": "no_op"},
                "allowed_to_apply": True,
                "requires_approval": True,
            },
        )
        monkeypatch.setattr(
            app_module,
            "execute_dml",
            lambda *_args, **_kwargs: pytest.fail("rename no-op must not execute DDL"),
        )

        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={
                "approved": True,
                "owner": "DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "rename_column",
                "new_name": "GIVEN_NAME",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] is False
        assert body["status"] == "NO_OP"

    def test_schema_apply_db_rename_table_carries_select_grants(self, app_client, monkeypatch):
        import app as app_module

        executed = []

        monkeypatch.setattr(
            app_module,
            "_schema_change_preflight_payload",
            lambda body: {
                "read_only": True,
                "operation": "rename_table",
                "object": {"owner": "DWH", "table": "ODS_OLD", "column": None},
                "requested": {"new_type": None, "new_name": "ODS_NEW"},
                "risk": {"classification": "review_required"},
                "allowed_to_apply": True,
                "requires_approval": True,
            },
        )
        monkeypatch.setattr(
            app_module,
            "_get_object_grants",
            lambda owner, table: [{"grantee": "APP_USER", "privilege": "SELECT"}],
        )
        monkeypatch.setattr(app_module, "execute_dml", lambda sql: executed.append(sql) or "OK")

        resp = app_client.post(
            "/api/schema/change/apply-db",
            json={
                "approved": True,
                "owner": "DWH",
                "table": "ODS_OLD",
                "op": "rename_table",
                "new_name": "ODS_NEW",
            },
        )

        assert resp.status_code == 200
        assert executed == [
            'ALTER TABLE "DWH"."ODS_OLD" RENAME TO "ODS_NEW"',
            'GRANT SELECT ON "DWH"."ODS_NEW" TO "APP_USER"',
        ]

    def test_detect_schema_changes_endpoint_returns_detected_count(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_detect(owner):
            calls.append(owner)
            return [{"owner": owner, "table": "ODS_STATUS", "change_type": "metadata_changed"}]

        monkeypatch.setattr(app_module, "detect_changes", fake_detect)

        resp = app_client.post(
            "/api/schema-changes/detect", json={"approved": True, "owner": "DWH"}
        )

        assert resp.status_code == 200
        assert calls == ["DWH"]
        assert resp.get_json() == {
            "detected": 1,
            "changes": [{"owner": "DWH", "table": "ODS_STATUS", "change_type": "metadata_changed"}],
        }

    def test_detect_schema_changes_requires_approval_before_detection(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "detect_changes",
            lambda *_args, **_kwargs: pytest.fail(
                "approval must be checked before schema hash persistence"
            ),
        )

        resp = app_client.post("/api/schema-changes/detect", json={"owner": "DWH"})

        assert resp.status_code == 400
        assert resp.get_json() == {
            "error": "approved:true is required for schema change detection."
        }

    def test_watermark_delta_endpoint_returns_query_without_execution(self, app_client, monkeypatch):
        import app as app_module

        class FakeWatermarkManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_delta_query(self, owner, table, date_column):
                assert owner == "DWH"
                assert table == "ODS_STATUS"
                assert date_column == "UPDATED_AT"
                return 'SELECT * FROM "DWH"."ODS_STATUS" WHERE "UPDATED_AT" > :last_value'

        monkeypatch.setattr(app_module, "WatermarkManager", FakeWatermarkManager)

        resp = app_client.post(
            "/api/watermarks/delta",
            json={"owner": "DWH", "table": "ODS_STATUS", "date_column": "UPDATED_AT"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {
            "delta_sql": 'SELECT * FROM "DWH"."ODS_STATUS" WHERE "UPDATED_AT" > :last_value',
            "table": "DWH.ODS_STATUS",
        }

    def test_schema_get_object_grants_filters_by_table_schema(self, monkeypatch):
        import pandas as pd
        import app as app_module

        captured = []

        def fake_execute_select(sql):
            captured.append(sql)
            return pd.DataFrame([
                {"GRANTEE": "APP_USER", "PRIVILEGE": "SELECT"},
            ])

        monkeypatch.setattr(app_module, "execute_select", fake_execute_select)

        grants = app_module._get_object_grants("DWH", "ODI41_EVID_RENAME_TAB")

        assert grants == [{"grantee": "APP_USER", "privilege": "SELECT"}]
        assert "TABLE_SCHEMA = 'DWH'" in captured[0]
        assert "OWNER = 'DWH'" not in captured[0]

    def test_schema_sync_odi_requires_approved_true(self, app_client):
        resp = app_client.post(
            "/api/schema/change/sync-odi",
            json={
                "model_code": "TGT_DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(30)",
            },
        )

        assert resp.status_code == 400
        assert "approved" in resp.get_json()["error"].lower()

    def test_schema_sync_odi_calls_orchestrator_metadata_sync(self, app_client, monkeypatch):
        import app as app_module
        import odi_orchestrator

        calls = []

        class FakeOrchestrator:
            def sync_datastore_column(self, **kwargs):
                calls.append(kwargs)
                return "SUCCESS: ODI datastore metadata synced"

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOrchestrator)

        resp = app_client.post(
            "/api/schema/change/sync-odi",
            json={
                "approved": True,
                "model_code": "TGT_DWH",
                "table": "ODS_HR_EMPLOYEES",
                "column": "FIRST_NAME",
                "op": "modify_type",
                "new_type": "VARCHAR2(30)",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] is True
        assert calls == [
            {
                "model_code": "TGT_DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "operation": "modify_type",
                "column_name": "FIRST_NAME",
                "column_type": "VARCHAR2",
                "column_length": 30,
                "column_scale": None,
                "new_name": None,
            }
        ]

    def test_schema_reconcile_scenario_requires_approved_true(self, app_client):
        resp = app_client.post(
            "/api/schema/change/reconcile-scenario",
            json={"mapping_name": "MAP_HR_EMPLOYEES_TO_ODS"},
        )

        assert resp.status_code == 400
        assert "approved" in resp.get_json()["error"].lower()

    def test_schema_reconcile_scenario_generates_metadata_and_probes_without_run(self, app_client, monkeypatch):
        import odi_orchestrator

        calls = []

        class FakeOrchestrator:
            def generate_scenario_metadata(self, **kwargs):
                calls.append(("generate", kwargs))
                return {"status": "success", "scenario_name": kwargs["scenario_name"]}

            def scenario_probe(self, **kwargs):
                calls.append(("probe", kwargs))
                return {"status": "success", "scenario_name": kwargs["scenario_name"]}

            def run_scenario(self, *_args, **_kwargs):
                pytest.fail("schema reconcile must not run the scenario")

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOrchestrator)

        resp = app_client.post(
            "/api/schema/change/reconcile-scenario",
            json={
                "approved": True,
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "scenario_version": "001",
                "project": "ORA_DWH",
                "folder": "HR",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] is True
        assert body["scenario_name"] == "MAP_HR_EMPLOYEES_TO_ODS_SCN"
        assert calls == [
            (
                "generate",
                {
                    "approved": True,
                    "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                    "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN",
                    "version": "001",
                    "project": "ORA_DWH",
                    "folder": "HR",
                },
            ),
            (
                "probe",
                {
                    "approved": True,
                    "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN",
                    "version": "001",
                },
            ),
        ]

    def test_schema_reconcile_scenario_reports_generation_failure(self, app_client, monkeypatch):
        import odi_orchestrator

        calls = []

        class FakeOrchestrator:
            def generate_scenario_metadata(self, **kwargs):
                calls.append(("generate", kwargs))
                return {"status": "error", "error": "RuntimeException"}

            def scenario_probe(self, **kwargs):
                calls.append(("probe", kwargs))
                pytest.fail("scenario probe must not run after failed generation")

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOrchestrator)

        resp = app_client.post(
            "/api/schema/change/reconcile-scenario",
            json={
                "approved": True,
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "scenario_version": "001",
            },
        )

        assert resp.status_code == 500
        body = resp.get_json()
        assert body["applied"] is False
        assert body["status"] == "SCENARIO_RECONCILE_FAILED"
        assert body["generation"]["status"] == "error"
        assert calls[0][0] == "generate"

    def test_schema_reconcile_scenario_treats_already_ready_as_success(self, app_client, monkeypatch):
        import odi_orchestrator

        calls = []

        class FakeOrchestrator:
            def generate_scenario_metadata(self, **kwargs):
                calls.append(("generate", kwargs))
                return {"status": "already_ready", "scenario": {"name": kwargs["scenario_name"]}}

            def scenario_probe(self, **kwargs):
                calls.append(("probe", kwargs))
                return {"status": "passed", "scenario": {"name": kwargs["scenario_name"]}}

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOrchestrator)

        resp = app_client.post(
            "/api/schema/change/reconcile-scenario",
            json={
                "approved": True,
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "scenario_version": "001",
            },
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["applied"] is True
        assert body["generation"]["status"] == "already_ready"
        assert calls[1][0] == "probe"


class TestOdiMappingApi:
    def test_odi_sessions_returns_recent_sessions_json_string(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_recent_sessions(limit=50):
            calls.append(limit)
            return json.dumps([{"session": "metadata-only"}])

        monkeypatch.setattr(app_module, "odi_get_recent_sessions", fake_recent_sessions)

        resp = app_client.get("/api/odi/sessions")

        assert resp.status_code == 200
        assert calls == [50]
        assert resp.mimetype == "application/json"
        assert resp.get_json() == [{"session": "metadata-only"}]

    def test_odi_session_suggestion_returns_none_without_error_message(self, app_client, monkeypatch):
        import sys
        import types

        fake_rag_manager = types.SimpleNamespace(
            search_documents=lambda *_args, **_kwargs: pytest.fail("RAG must not run without an error message")
        )
        monkeypatch.setitem(sys.modules, "rag_manager", fake_rag_manager)

        resp = app_client.post("/api/odi/session_suggestion", json={"error_message": ""})

        assert resp.status_code == 200
        assert resp.get_json() == {"suggestion": None}

    def test_odi_session_suggestion_returns_first_rag_match(self, app_client, monkeypatch):
        import sys
        import types

        calls = []

        def fake_search_documents(collection, query, n_results):
            calls.append((collection, query, n_results))
            return [{"title": "Known ODI error", "resolution": "Retry after repository check"}]

        fake_rag_manager = types.SimpleNamespace(search_documents=fake_search_documents)
        monkeypatch.setitem(sys.modules, "rag_manager", fake_rag_manager)

        resp = app_client.post("/api/odi/session_suggestion", json={"error_message": "ODI-1590"})

        assert resp.status_code == 200
        assert calls == [("error_patterns", "ODI-1590", 1)]
        assert resp.get_json() == {
            "suggestion": {"title": "Known ODI error", "resolution": "Retry after repository check"}
        }

    def test_odi_scenarios_returns_scenario_list_json_string(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_scenario_list():
            calls.append(True)
            return json.dumps([{"scenario": "metadata-only"}])

        monkeypatch.setattr(app_module, "odi_get_scenario_list", fake_scenario_list)

        resp = app_client.get("/api/odi/scenarios")

        assert resp.status_code == 200
        assert calls == [True]
        assert resp.mimetype == "application/json"
        assert resp.get_json() == [{"scenario": "metadata-only"}]

    def test_odi_mappings_groups_folder_and_project_metadata(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "odi_get_mapping_lineage",
            lambda limit=100: json.dumps([
                {
                    "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                    "PROJECT_CODE": "ORA_DWH",
                    "PROJECT_NAME": "Oracle DWH",
                    "FOLDER_NAME": "FLD_CORE_MAPPINGS",
                    "COMP_NAME": "EMPLOYEES",
                    "COMP_TYPE": "DATASTORE",
                    "TABLE_QUALIFIED_NAME": "HR.EMPLOYEES",
                },
                {
                    "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                    "PROJECT_CODE": "ORA_DWH",
                    "PROJECT_NAME": "Oracle DWH",
                    "FOLDER_NAME": "FLD_CORE_MAPPINGS",
                    "COMP_NAME": "ODS_HR_EMPLOYEES",
                    "COMP_TYPE": "DATASTORE",
                    "TABLE_QUALIFIED_NAME": "DWH.ODS_HR_EMPLOYEES",
                },
            ]),
        )

        resp = app_client.get("/api/odi/mappings")

        assert resp.status_code == 200
        [mapping] = resp.get_json()
        assert mapping["folder_name"] == "FLD_CORE_MAPPINGS"
        assert mapping["FOLDER_NAME"] == "FLD_CORE_MAPPINGS"
        assert mapping["project_code"] == "ORA_DWH"
        assert mapping["project"] == "ORA_DWH"

    def test_odi_lineage_returns_mapping_lineage_json_string(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_mapping_lineage(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return json.dumps([{"lineage": "metadata-only"}])

        monkeypatch.setattr(app_module, "odi_get_mapping_lineage", fake_mapping_lineage)

        resp = app_client.get("/api/odi/lineage")

        assert resp.status_code == 200
        assert calls == [{"args": (), "kwargs": {}}]
        assert resp.mimetype == "application/json"
        assert resp.get_json() == [{"lineage": "metadata-only"}]

    def test_odi_kms_returns_knowledge_modules_json_string(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_knowledge_modules():
            calls.append(True)
            return json.dumps([{"km": "metadata-only"}])

        monkeypatch.setattr(app_module, "odi_get_knowledge_modules", fake_knowledge_modules)

        resp = app_client.get("/api/odi/kms")

        assert resp.status_code == 200
        assert calls == [True]
        assert resp.mimetype == "application/json"
        assert resp.get_json() == [{"km": "metadata-only"}]

    def test_mapping_detail_returns_expression_rows_and_source_columns(self, app_client, monkeypatch):
        import app as app_module

        detail_rows = [
            {
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                "COMP_NAME": "EMPLOYEES",
                "COMP_TYPE": "DATASTORE",
                "TABLE_QUALIFIED_NAME": "HR.EMPLOYEES",
                "ATTR_NAME": "EMPLOYEE_ID",
                "EXPRESSION": None,
                "DATA_TYPE": "NUMBER",
                "COL_LENGTH": None,
                "COL_PREC": "6",
                "COL_SCALE": "0",
            },
            {
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                "COMP_NAME": "ODS_HR_EMPLOYEES",
                "COMP_TYPE": "DATASTORE",
                "TABLE_QUALIFIED_NAME": "DWH.ODS_HR_EMPLOYEES",
                "ATTR_NAME": "EMPLOYEE_ID",
                "EXPRESSION": "EMPLOYEES.EMPLOYEE_ID",
                "DATA_TYPE": "NUMBER",
                "COL_LENGTH": None,
                "COL_PREC": "6",
                "COL_SCALE": "0",
            },
            {
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                "COMP_NAME": "FLT_EMPLOYEES",
                "COMP_TYPE": "FILTER",
                "TABLE_QUALIFIED_NAME": None,
                "ATTR_NAME": "FILTER_CONDITION",
                "EXPRESSION": "EMPLOYEES.EMPLOYEE_ID > 0",
                "DATA_TYPE": None,
                "COL_LENGTH": None,
                "COL_PREC": None,
                "COL_SCALE": None,
            },
        ]

        monkeypatch.setattr(
            app_module,
            "odi_get_mapping_details",
            lambda mapping_name: json.dumps(detail_rows),
        )
        monkeypatch.setattr(
            app_module,
            "get_table_schema",
            lambda table_name: pd.DataFrame([
                {"COLUMN_NAME": "EMPLOYEE_ID", "FULL_DATA_TYPE": "NUMBER(6)"}
            ]),
        )

        resp = app_client.get("/api/odi/mapping_detail?mapping_name=MAP_HR_EMPLOYEES_TO_ODS")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["expression_rows"] == [
            {
                "target_column": "EMPLOYEE_ID",
                "expression": "EMPLOYEES.EMPLOYEE_ID",
                "data_type": "NUMBER(6)",
                "is_expression": False,
            }
        ]
        assert body["source_columns"] == [
            {
                "table": "HR.EMPLOYEES",
                "component": "EMPLOYEES",
                "column": "EMPLOYEE_ID",
                "data_type": "NUMBER(6)",
            }
        ]
        assert body["filters"] == ["EMPLOYEES.EMPLOYEE_ID > 0"]

    def test_mapping_detail_enriches_km_options_from_read_only_repository_probe(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "odi_get_mapping_details",
            lambda mapping_name: json.dumps([
                {
                    "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                    "COMP_NAME": "ODS_HR_EMPLOYEES",
                    "COMP_TYPE": "DATASTORE",
                    "TABLE_QUALIFIED_NAME": "DWH.ODS_HR_EMPLOYEES",
                    "ATTR_NAME": "EMPLOYEE_ID",
                    "EXPRESSION": "EMPLOYEES.EMPLOYEE_ID",
                    "DATA_TYPE": "NUMBER",
                    "COL_LENGTH": None,
                    "COL_PREC": "6",
                    "COL_SCALE": "0",
                }
            ]),
        )
        monkeypatch.setattr(
            app_module,
            "_read_mapping_km_options",
            lambda mapping_name: {
                "status": "passed",
                "read_only": True,
                "truncate_target_table": True,
                "ikm_name": "IKM Oracle Insert.GLOBAL",
                "lkm_name": "LKM SQL to Oracle",
                "target_node": "ODS_HR_EMPLOYEES",
            },
        )
        monkeypatch.setattr(
            app_module,
            "get_table_schema",
            lambda table_name: pd.DataFrame([
                {"COLUMN_NAME": "EMPLOYEE_ID", "FULL_DATA_TYPE": "NUMBER(6)"}
            ]),
        )

        resp = app_client.get("/api/odi/mapping_detail?mapping_name=MAP_HR_EMPLOYEES_TO_ODS")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["km_options"] == {
            "status": "passed",
            "read_only": True,
            "truncate_target_table": True,
            "ikm_name": "IKM Oracle Insert.GLOBAL",
            "lkm_name": "LKM SQL to Oracle",
            "target_node": "ODS_HR_EMPLOYEES",
        }


class TestStatusEndpoint:
    """GET /api/status tests."""

    def test_status_returns_200(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 200

    def test_status_json_structure(self, app_client):
        resp = app_client.get("/api/status")
        data = resp.get_json()
        assert "ok" in data
        assert "checks" in data
        assert "count" in data
        # count format: "X/8"
        assert "/" in data["count"]

    def test_status_uses_health_checkers_without_shape_change(self, app_client, monkeypatch):
        import app as app_module

        checker_values = {
            "_chk_oracle": True,
            "_chk_source": True,
            "_chk_mssql": True,
            "_chk_rag": True,
            "_chk_llm": True,
            "_chk_local_openai": True,
            "_chk_claude": True,
            "_chk_docker": True,
            "_chk_mcp": True,
            "_chk_monitoring": True,
            "_chk_dba": True,
            "_chk_graph_health": True,
            "_chk_ai_orchestrator": True,
        }
        for name, value in checker_values.items():
            monkeypatch.setattr(app_module, name, lambda value=value: value)

        resp = app_client.get("/api/status")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["count"] == "13/13"
        assert set(data["checks"]) == {
            "oracle",
            "source",
            "mssql",
            "rag",
            "llm",
            "local_openai",
            "claude",
            "docker",
            "mcp",
            "monitoring",
            "dba",
            "langgraph",
            "ai_orchestrator",
        }
        assert "llm_provider" in data


class TestAlertEndpoints:
    """Alert API tests."""

    def test_alerts_returns_200(self, app_client):
        resp = app_client.get("/api/alerts")
        assert resp.status_code == 200

    def test_alerts_returns_list(self, app_client):
        resp = app_client.get("/api/alerts")
        data = resp.get_json()
        assert isinstance(data, list)

    def test_alerts_history_returns_200(self, app_client):
        resp = app_client.get("/api/alerts/history")
        assert resp.status_code == 200


class TestAlertWriteEndpoints:
    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/api/alerts/acknowledge", {"all": True}),
            ("/api/alerts/check", {"freshness_days": 3}),
        ],
    )
    def test_alert_writes_require_approval_before_manager_construction(
        self, app_client, monkeypatch, path, payload
    ):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "AlertManager",
            lambda: pytest.fail("approval must be checked before AlertManager construction"),
        )

        resp = app_client.post(path, json=payload)

        assert resp.status_code == 400
        assert "approved:true is required" in resp.get_json()["error"]

    def test_alert_acknowledge_single_uses_alert_manager(self, app_client, monkeypatch):
        import app as app_module

        acknowledged = []

        class FakeAlertManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def acknowledge(self, alert_id):
                acknowledged.append(alert_id)
                return True

        monkeypatch.setattr(app_module, "AlertManager", FakeAlertManager)

        resp = app_client.post(
            "/api/alerts/acknowledge", json={"approved": True, "alert_id": "42"}
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert acknowledged == [42]

    def test_alert_check_uses_alert_manager(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeAlertManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def check_and_alert(self, *, freshness_days):
                calls.append(freshness_days)
                return [{"alert_type": "TEST_FRESHNESS"}, {"alert_type": "TEST_SCHEMA"}]

        monkeypatch.setattr(app_module, "AlertManager", FakeAlertManager)

        resp = app_client.post(
            "/api/alerts/check", json={"approved": True, "freshness_days": 3}
        )

        assert resp.status_code == 200
        assert resp.get_json() == {
            "created_alerts": [{"alert_type": "TEST_FRESHNESS"}, {"alert_type": "TEST_SCHEMA"}],
            "count": 2,
        }
        assert calls == [3]


class TestGeneralUtilityWriteEndpoints:
    def test_execute_select_uses_limited_preview(self, app_client, monkeypatch):
        import app as app_module

        captured = []

        def fake_execute_select(sql):
            captured.append(sql)
            return pd.DataFrame({"COL_A": [1, 2]})

        monkeypatch.setattr(app_module, "execute_select", fake_execute_select)

        resp = app_client.post(
            "/api/execute",
            json={"sql": "SELECT COL_A FROM DWH.TEST_TABLE;", "label": "Unit Preview"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["result"] == "Query returned 2 row(s)."
        assert data["query"]["action"] == "select"
        assert data["query"]["label"] == "Unit Preview"
        assert data["query"]["sql"] == "SELECT COL_A FROM DWH.TEST_TABLE"
        assert data["query"]["preview_limited"] is True
        assert data["query"]["preview_rows"] == 100
        assert data["query"]["result"] == {
            "columns": ["COL_A"],
            "rows": [{"COL_A": 1}, {"COL_A": 2}],
        }
        assert len(captured) == 1
        assert "ROWNUM <= 100" in captured[0]

    def test_execute_dml_calls_executor_after_approval(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_execute_dml(sql, params=None):
            calls.append((sql, params))
            return "DML completed"

        monkeypatch.setattr(app_module, "execute_dml", fake_execute_dml)

        resp = app_client.post(
            "/api/execute",
            json={
                "sql": "UPDATE DWH.TEST_TABLE SET COL_A = :value",
                "params": {"value": 1},
                "approved": True,
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"result": "DML completed"}
        assert calls == [("UPDATE DWH.TEST_TABLE SET COL_A = :value", {"value": 1})]

    def test_export_returns_excel_attachment(self, app_client, monkeypatch):
        import app as app_module
        import routes.misc_write as misc_write

        calls = []

        class FakeDataFrame:
            def __init__(self, rows, columns):
                calls.append(("dataframe", rows, columns))

            def to_excel(self, writer, *, index, sheet_name):
                calls.append(("to_excel", writer.engine, index, sheet_name))

        class FakeExcelWriter:
            def __init__(self, buf, engine):
                self.buf = buf
                self.engine = engine

            def __enter__(self):
                calls.append(("enter_writer", self.engine))
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                calls.append(("exit_writer", self.engine))
                return False

        def fake_send_file(buf, *, mimetype, as_attachment, download_name):
            calls.append(("send_file", mimetype, as_attachment, download_name.startswith("report_")))
            return app_module.app.response_class(
                response=b"exported",
                status=200,
                mimetype=mimetype,
                headers={"Content-Disposition": f"attachment; filename={download_name}"},
            )

        monkeypatch.setattr(misc_write.pd, "DataFrame", FakeDataFrame)
        monkeypatch.setattr(misc_write.pd, "ExcelWriter", FakeExcelWriter)
        monkeypatch.setattr(misc_write, "send_file", fake_send_file)

        resp = app_client.post(
            "/api/export",
            json={"headers": ["COL_A"], "rows": [[1]]},
        )

        assert resp.status_code == 200
        assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert resp.headers["Content-Disposition"].startswith("attachment;")
        assert calls == [
            ("dataframe", [[1]], ["COL_A"]),
            ("enter_writer", "openpyxl"),
            ("to_excel", "openpyxl", False, "Report"),
            ("exit_writer", "openpyxl"),
            (
                "send_file",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                True,
                True,
            ),
        ]

    def test_clear_empties_session(self, app_client):
        with app_client.session_transaction() as sess:
            sess["last_query"] = "metadata-only"

        resp = app_client.post("/api/clear")

        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        with app_client.session_transaction() as sess:
            assert "last_query" not in sess


class TestMonitoringLineageWriteEndpoints:
    def test_monitoring_dq_scores_uses_mon_dq_score(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        def fake_mon_dq_score(*, owner, table_name):
            calls.append((owner, table_name))
            return '[{"table_name": "TEST_TABLE", "score": 100}]'

        monkeypatch.setattr(app_module, "mon_dq_score", fake_mon_dq_score)

        resp = app_client.get("/api/monitoring/dq-scores?owner=dwh&table=test_table")

        assert resp.status_code == 200
        assert resp.mimetype == "application/json"
        assert resp.get_json() == [{"table_name": "TEST_TABLE", "score": 100}]
        assert calls == [("DWH", "TEST_TABLE")]

    def test_lineage_refresh_uses_lineage_manager(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeLineageManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def populate_from_odi(self):
                calls.append("populate")
                return {"status": "ok", "loaded_count": 0}

        monkeypatch.setattr(app_module, "LineageManager", FakeLineageManager)

        resp = app_client.post("/api/lineage/refresh", json={"approved": True})

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok", "loaded_count": 0}
        assert calls == ["populate"]

    def test_lineage_refresh_requires_approval_before_manager_construction(
        self, app_client, monkeypatch
    ):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "LineageManager",
            lambda: pytest.fail("approval must be checked before LineageManager construction"),
        )

        resp = app_client.post("/api/lineage/refresh", json={})

        assert resp.status_code == 400
        assert resp.get_json() == {
            "error": "approved:true is required for lineage refresh."
        }


class TestLegacyAirflowWriteEndpoints:
    def test_airflow_run_odi_uses_remote_adapter_after_approval(self, app_client, monkeypatch):
        import odi_orchestrator
        from tests.test_remote_run_contract import done

        calls = []

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def run_scenario_remote(self, **kwargs):
                calls.append(kwargs)
                return done()

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/airflow/run-odi",
            json={"approved": True, "scen_name": "MAP_TEST_SCN"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == done()
        assert len(calls) == 1
        assert calls[0] == {"approved": True, "scenario_name": "MAP_TEST_SCN", "mapping_name": "MAP_TEST",
                            "version": "001", "context": "GLOBAL"}

    def test_airflow_run_mapping_uses_orchestrator_after_approval(self, app_client, monkeypatch):
        import odi_orchestrator
        from tests.test_remote_run_contract import done

        calls = []

        class FakeOdiOrchestrator(_RemoteRunDouble):
            def remote_agent_health(self, **kwargs):
                calls.append(("health",))
                return super().remote_agent_health(**kwargs)

            def generate_scenario_metadata(self, **kwargs):
                calls.append(("generate", kwargs["scenario_name"], kwargs["mapping_name"], kwargs["version"]))
                return super().generate_scenario_metadata(**kwargs)

            def run_scenario_remote(self, **kwargs):
                calls.append(("run", kwargs["scenario_name"], kwargs["version"]))
                return done()

        monkeypatch.setattr(odi_orchestrator, "OdiOrchestrator", FakeOdiOrchestrator)

        resp = app_client.post(
            "/api/airflow/run-mapping",
            json={"approved": True, "mapping_name": "MAP_TEST", "scenario_version": "002"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == done()
        assert calls == [
            ("health",),
            ("generate", "MAP_TEST_SCN", "MAP_TEST", "002"),
            ("run", "MAP_TEST_SCN", "002"),
        ]

    def test_airflow_run_dq_uses_data_quality_after_approval(self, app_client, monkeypatch):
        import app as app_module

        calls = []

        class FakeResult:
            def summary(self):
                return {"validated": 1}

        class FakeDataQualityManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def validate_and_insert_errors(self, owner, table, *, max_errors_per_rule):
                calls.append((owner, table, max_errors_per_rule))
                return FakeResult()

        monkeypatch.setattr(app_module, "DataQualityManager", FakeDataQualityManager)

        resp = app_client.post(
            "/api/airflow/run-dq",
            json={"approved": True, "owner": "dwh", "table": "ods_status"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"validated": 1}
        assert calls == [("DWH", "ODS_STATUS", 100)]


class TestTablesEndpoint:
    """GET /api/tables tests."""

    def test_tables_returns_200(self, app_client):
        resp = app_client.get("/api/tables")
        assert resp.status_code == 200

    def test_tables_returns_list(self, app_client):
        resp = app_client.get("/api/tables")
        data = resp.get_json()
        assert isinstance(data, list)


class TestMonitoringEndpoints:
    """Monitoring API tests."""

    def test_freshness_returns_200(self, app_client):
        resp = app_client.get("/api/monitoring/freshness")
        assert resp.status_code == 200

    def test_dq_scores_returns_200(self, app_client):
        resp = app_client.get("/api/monitoring/dq-scores?owner=DWH")
        assert resp.status_code == 200


class TestLineageUsageEndpoint:
    def test_lineage_usage_requires_mapping_name(self, app_client):
        resp = app_client.get("/api/lineage/usage/HR.EMPLOYEES")

        assert resp.status_code == 400
        assert resp.get_json() == {"error": "mapping_name is required."}

    def test_lineage_usage_enriches_detail_data_type_without_live_services(self, app_client, monkeypatch):
        import app as app_module

        class FakeLineageManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_table_usage_details(self, table_name, mapping_name):
                assert table_name == "HR.EMPLOYEES"
                assert mapping_name == "MAP_HR_EMPLOYEES_TO_ODS"
                return [
                    {
                        "attr_name": "EMPLOYEE_ID",
                        "expression": "EMPLOYEES.EMPLOYEE_ID",
                    }
                ]

        monkeypatch.setattr(app_module, "LineageManager", FakeLineageManager)
        monkeypatch.setattr(
            app_module,
            "odi_get_mapping_details",
            lambda mapping_name: json.dumps([
                {
                    "COMP_NAME": "EMPLOYEES",
                    "COMP_TYPE": "DATASTORE",
                    "TABLE_QUALIFIED_NAME": "HR.EMPLOYEES",
                    "ATTR_NAME": "EMPLOYEE_ID",
                    "DATA_TYPE": "NUMBER",
                    "DATA_LENGTH": None,
                    "DATA_PRECISION": "6",
                    "DATA_SCALE": "0",
                },
                {
                    "COMP_NAME": "ODS_HR_EMPLOYEES",
                    "COMP_TYPE": "DATASTORE",
                    "TABLE_QUALIFIED_NAME": "DWH.ODS_HR_EMPLOYEES",
                    "ATTR_NAME": "EMPLOYEE_ID",
                    "DATA_TYPE": "NUMBER",
                    "DATA_LENGTH": None,
                    "DATA_PRECISION": "6",
                    "DATA_SCALE": "0",
                },
            ]),
        )
        monkeypatch.setattr(
            app_module,
            "get_table_schema",
            lambda table_name: pd.DataFrame([
                {"COLUMN_NAME": "EMPLOYEE_ID", "FULL_DATA_TYPE": "NUMBER(6)"}
            ]),
        )

        resp = app_client.get("/api/lineage/usage/HR.EMPLOYEES?mapping_name=MAP_HR_EMPLOYEES_TO_ODS")

        assert resp.status_code == 200
        assert resp.get_json() == [
            {
                "attr_name": "EMPLOYEE_ID",
                "expression": "EMPLOYEES.EMPLOYEE_ID",
                "data_type": "NUMBER(6)",
            }
        ]


class TestMappingSnapshotReadEndpoints:
    def test_mapping_snapshots_passes_limit_to_manager(self, app_client, monkeypatch):
        import app as app_module

        class FakeMappingSnapshotManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_snapshots(self, mapping_name, limit=10):
                assert mapping_name == "MAP_HR_EMPLOYEES_TO_ODS"
                assert limit == 3
                return [{"snap_id": 101, "mapping_name": mapping_name}]

        monkeypatch.setattr(app_module, "MappingSnapshotManager", FakeMappingSnapshotManager)

        resp = app_client.get("/api/mapping/snapshots/MAP_HR_EMPLOYEES_TO_ODS?limit=3")

        assert resp.status_code == 200
        assert resp.get_json() == [{"snap_id": 101, "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS"}]

    def test_mapping_compare_uses_explicit_snapshot_ids(self, app_client, monkeypatch):
        import app as app_module

        class FakeMappingSnapshotManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def compare_snapshots(self, snap_old, snap_new):
                assert snap_old == 10
                assert snap_new == 11
                return {"mode": "explicit", "snap_old": snap_old, "snap_new": snap_new}

            def compare_latest(self, mapping_name):
                raise AssertionError("explicit snapshot ids must not use compare_latest")

        monkeypatch.setattr(app_module, "MappingSnapshotManager", FakeMappingSnapshotManager)

        resp = app_client.get("/api/mapping/compare/MAP_HR_EMPLOYEES_TO_ODS?snap_old=10&snap_new=11")

        assert resp.status_code == 200
        assert resp.get_json() == {"mode": "explicit", "snap_old": 10, "snap_new": 11}

    def test_mapping_compare_falls_back_to_latest_when_ids_missing(self, app_client, monkeypatch):
        import app as app_module

        class FakeMappingSnapshotManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def compare_snapshots(self, snap_old, snap_new):
                raise AssertionError("missing snapshot ids must use compare_latest")

            def compare_latest(self, mapping_name):
                assert mapping_name == "MAP_HR_EMPLOYEES_TO_ODS"
                return {"mode": "latest", "mapping_name": mapping_name}

        monkeypatch.setattr(app_module, "MappingSnapshotManager", FakeMappingSnapshotManager)

        resp = app_client.get("/api/mapping/compare/MAP_HR_EMPLOYEES_TO_ODS")

        assert resp.status_code == 200
        assert resp.get_json() == {"mode": "latest", "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS"}


class TestQualityEndpoints:
    """DQ API tests."""

    def test_validate_runs_generated_and_extra_rules(self, app_client, monkeypatch):
        import app as app_module

        class FakeResult:
            def summary(self):
                return {"status": "passed", "rule_count": 1}

        class FakeDataQualityManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def generate_rules(self, owner, table):
                assert owner == "DWH"
                assert table == "ODS_STATUS"
                return []

            def validate_table(self, owner, table, custom_rules):
                assert owner == "DWH"
                assert table == "ODS_STATUS"
                assert len(custom_rules) == 1
                assert custom_rules[0].column == "STATUS_CODE"
                return FakeResult()

        monkeypatch.setattr(app_module, "DataQualityManager", FakeDataQualityManager)

        resp = app_client.post(
            "/api/quality/validate",
            json={
                "owner": "DWH",
                "table": "ODS_STATUS",
                "extra_rules": [{"column": "STATUS_CODE", "rule_type": "not_null"}],
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "passed", "rule_count": 1}

    def test_validate_requires_table(self, app_client):
        resp = app_client.post(
            "/api/quality/validate",
            json={"owner": "DWH"},
        )
        assert resp.status_code == 400

    def test_reconcile_calls_reconciliator(self, app_client, monkeypatch):
        import reconciliator

        monkeypatch.setattr(
            reconciliator,
            "reconcile_table",
            lambda owner, table: {"status": "passed", "owner": owner, "table": table},
        )

        resp = app_client.post("/api/quality/reconcile", json={"owner": "DWH", "table": "ODS_STATUS"})

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "passed", "owner": "DWH", "table": "ODS_STATUS"}

    def test_autofix_column_calls_reconciliator(self, app_client, monkeypatch):
        import reconciliator

        monkeypatch.setattr(
            reconciliator,
            "autofix_missing_column",
            lambda owner, table, column: {"fixed": True, "owner": owner, "table": table, "column": column},
        )

        resp = app_client.post(
            "/api/quality/autofix-column",
            json={"approved": True, "owner": "DWH", "table": "ODS_STATUS", "column": "STATUS_CODE"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {
            "fixed": True,
            "owner": "DWH",
            "table": "ODS_STATUS",
            "column": "STATUS_CODE",
        }

    def test_autofix_all_columns_calls_reconciliator(self, app_client, monkeypatch):
        import reconciliator

        monkeypatch.setattr(
            reconciliator,
            "autofix_all_missing_columns",
            lambda owner, table, columns: {"fixed_count": len(columns), "owner": owner, "table": table},
        )

        resp = app_client.post(
            "/api/quality/autofix-all-columns",
            json={
                "approved": True,
                "owner": "DWH",
                "table": "ODS_STATUS",
                "columns": ["STATUS_CODE", "STATUS_DESC"],
            },
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"fixed_count": 2, "owner": "DWH", "table": "ODS_STATUS"}

    def test_sync_mapping_calls_reconciliator(self, app_client, monkeypatch):
        import reconciliator

        monkeypatch.setattr(
            reconciliator,
            "sync_odi_mapping",
            lambda owner, table: {"synced": True, "owner": owner, "table": table},
        )

        resp = app_client.post(
            "/api/quality/sync-mapping",
            json={"approved": True, "owner": "DWH", "table": "ODS_STATUS"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"synced": True, "owner": "DWH", "table": "ODS_STATUS"}

    def test_create_error_table_uses_dq_manager(self, app_client, monkeypatch):
        import app as app_module

        class FakeDataQualityManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def create_error_table(self, owner, table):
                assert owner == "DWH"
                assert table == "ODS_STATUS"

        monkeypatch.setattr(app_module, "DataQualityManager", FakeDataQualityManager)

        resp = app_client.post(
            "/api/quality/create-error-table",
            json={"approved": True, "owner": "DWH", "table": "ODS_STATUS"},
        )

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert resp.get_json()["error_table"].endswith(".ERR_ODS_STATUS")

    def test_create_all_error_tables_uses_dq_manager(self, app_client, monkeypatch):
        import app as app_module

        class FakeDataQualityManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def create_all_error_tables(self, owner):
                assert owner == "DWH"
                return {"created": 2}

        monkeypatch.setattr(app_module, "DataQualityManager", FakeDataQualityManager)

        resp = app_client.post(
            "/api/quality/create-all-error-tables",
            json={"approved": True, "owner": "DWH"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"created": 2}

    def test_validate_insert_uses_dq_manager(self, app_client, monkeypatch):
        import app as app_module

        class FakeResult:
            def summary(self):
                return {"status": "passed", "inserted_errors": 0}

        class FakeDataQualityManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def validate_and_insert_errors(self, owner, table, max_errors_per_rule=100):
                assert owner == "DWH"
                assert table == "ODS_STATUS"
                assert max_errors_per_rule == 7
                return FakeResult()

        monkeypatch.setattr(app_module, "DataQualityManager", FakeDataQualityManager)

        resp = app_client.post(
            "/api/quality/validate-insert",
            json={"approved": True, "owner": "DWH", "table": "ODS_STATUS", "max_errors_per_rule": 7},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "passed", "inserted_errors": 0}

    def test_validate_insert_requires_table(self, app_client):
        resp = app_client.post(
            "/api/quality/validate-insert",
            json={"approved": True, "owner": "DWH"},
        )
        assert resp.status_code == 400

    def test_persistent_quality_actions_require_strict_approval_before_delegation(
        self, app_client, monkeypatch
    ):
        import app as app_module
        import reconciliator

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("persistent helper must not run without approved:true")

        monkeypatch.setattr(reconciliator, "autofix_missing_column", must_not_run)
        monkeypatch.setattr(reconciliator, "autofix_all_missing_columns", must_not_run)
        monkeypatch.setattr(reconciliator, "sync_odi_mapping", must_not_run)
        monkeypatch.setattr(app_module, "DataQualityManager", must_not_run)
        monkeypatch.setattr(app_module, "SkeyManager", must_not_run)

        cases = [
            ("/api/quality/autofix-column", {"owner": "DWH", "table": "ODS_STATUS", "column": "STATUS_CODE"}),
            ("/api/quality/autofix-all-columns", {"owner": "DWH", "table": "ODS_STATUS", "columns": ["STATUS_CODE"]}),
            ("/api/quality/sync-mapping", {"owner": "DWH", "table": "ODS_STATUS"}),
            ("/api/quality/create-error-table", {"owner": "DWH", "table": "ODS_STATUS"}),
            ("/api/quality/create-all-error-tables", {"owner": "DWH"}),
            ("/api/quality/validate-insert", {"owner": "DWH", "table": "ODS_STATUS"}),
            ("/api/skey/create", {"owner": "DWH", "table": "DIM_STATUS"}),
        ]

        for path, payload in cases:
            for approved in (None, False, 1, "true"):
                body = dict(payload)
                if approved is not None:
                    body["approved"] = approved
                resp = app_client.post(path, json=body)
                assert resp.status_code == 400, path
                assert "approved:true" in resp.get_json()["error"]

    def test_quality_identifiers_are_rejected_before_delegation(self, app_client, monkeypatch):
        import app as app_module
        import reconciliator

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("invalid identifiers must be rejected before delegation")

        monkeypatch.setattr(reconciliator, "reconcile_table", must_not_run)
        monkeypatch.setattr(reconciliator, "autofix_missing_column", must_not_run)
        monkeypatch.setattr(app_module, "DataQualityManager", must_not_run)

        cases = [
            ("/api/quality/reconcile", {"owner": "DWH", "table": "BAD-NAME"}),
            (
                "/api/quality/autofix-column",
                {"approved": True, "owner": "DWH", "table": "ODS_STATUS", "column": "BAD-NAME"},
            ),
            (
                "/api/quality/validate-insert",
                {"approved": True, "owner": "DWH", "table": "BAD-NAME"},
            ),
        ]

        for path, payload in cases:
            resp = app_client.post(path, json=payload)
            assert resp.status_code == 400, path

    def test_quality_result_error_is_redacted(self, app_client, monkeypatch):
        import reconciliator

        monkeypatch.setattr(
            reconciliator,
            "sync_odi_mapping",
            lambda *_args: {"error": "internal driver detail must not reach the response"},
        )

        resp = app_client.post(
            "/api/quality/sync-mapping",
            json={"approved": True, "owner": "DWH", "table": "ODS_STATUS"},
        )

        assert resp.status_code == 500
        body = resp.get_json()
        assert body == {"error": "quality mapping sync failed.", "error_type": "OperationError"}
        assert "driver detail" not in resp.get_data(as_text=True)


class TestSkeyEndpoints:
    """SKEY API tests."""

    def test_skey_create_uses_manager(self, app_client, monkeypatch):
        import app as app_module

        class FakeSkeyManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def create_skey_objects(self, owner, table):
                assert owner == "DWH"
                assert table == "DIM_STATUS"
                return {"created": True}

        monkeypatch.setattr(app_module, "SkeyManager", FakeSkeyManager)

        resp = app_client.post(
            "/api/skey/create",
            json={"approved": True, "owner": "DWH", "table": "DIM_STATUS"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"created": True}

    def test_skey_create_requires_table(self, app_client):
        resp = app_client.post(
            "/api/skey/create",
            json={"approved": True, "owner": "DWH"},
        )
        assert resp.status_code == 400

    def test_skey_expression_uses_manager(self, app_client, monkeypatch):
        import app as app_module

        class FakeSkeyManager:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get_skey_expression(self, owner, table, nk_expr):
                assert owner == "DWH"
                assert table == "DIM_STATUS"
                assert nk_expr == "SRC.STATUS_CODE"
                return "CASE WHEN SRC.STATUS_CODE IS NULL THEN -1 ELSE 1 END"

        monkeypatch.setattr(app_module, "SkeyManager", FakeSkeyManager)

        resp = app_client.post(
            "/api/skey/expression",
            json={"owner": "DWH", "table": "DIM_STATUS", "nk_expr": "SRC.STATUS_CODE"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"expression": "CASE WHEN SRC.STATUS_CODE IS NULL THEN -1 ELSE 1 END"}


class TestEnvironmentEndpoints:
    def test_environment_endpoint_returns_redacted_runtime_identity(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module, "ENVIRONMENT", "dev")
        monkeypatch.setattr(app_module, "ORACLE_DSN_DISPLAY", "APP_USER/orcl")

        resp = app_client.get("/api/environment")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {"environment": "dev", "oracle_dsn": "APP_USER/orcl"}

    def test_project_hub_returns_filtered_architecture_with_statuses(self, app_client, monkeypatch, tmp_path):
        import socket

        import app as app_module
        import routes.environment as environment_routes

        network_calls = []

        def forbidden_network(*_args, **_kwargs):
            network_calls.append(True)
            raise AssertionError("Project-hub success test must not access the network")

        class FakeResponse:
            status_code = 200

        (tmp_path / "project_hub.json").write_text(
            json.dumps({
                "architecture": [
                    {"id": "oracle", "status": "Unknown"},
                    {"id": "ollama", "status": "Unknown"},
                    {"id": "docker", "status": "Unknown"},
                    {"id": "git", "status": "Disabled"},
                ],
                "tasks": [],
            }),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(app_module, "test_connection", lambda: True)
        monkeypatch.setattr(environment_routes, "ai_get", lambda *_args, **_kwargs: FakeResponse())
        monkeypatch.setattr(app_module.requests, "get", forbidden_network)
        monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
        monkeypatch.setattr(socket.socket, "connect", forbidden_network)

        resp = app_client.get("/api/project_hub")

        assert resp.status_code == 200
        body = resp.get_json()
        architecture = {item["id"]: item["status"] for item in body["architecture"]}
        assert "git" not in architecture
        assert architecture["oracle"] == "Active"
        assert architecture["ollama"] == "Active"
        assert architecture["docker"] == "Active"
        assert network_calls == []

    def test_project_hub_uses_english_down_status_text(self, app_client, monkeypatch, tmp_path):
        import socket

        import app as app_module
        import routes.environment as environment_routes

        transport_calls = []
        network_calls = []

        def forbidden_network(*_args, **_kwargs):
            network_calls.append(True)
            raise AssertionError("Project-hub Down test must not access the network")

        class FakeResponse:
            status_code = 503

        def fake_get(url, **kwargs):
            transport_calls.append((url, kwargs))
            return FakeResponse()

        (tmp_path / "project_hub.json").write_text(
            json.dumps({
                "architecture": [
                    {"id": "oracle", "status": "Unknown"},
                    {"id": "ollama", "status": "Unknown"},
                ],
            }),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(app_module, "test_connection", lambda: False)
        monkeypatch.setattr(environment_routes, "ai_get", fake_get)
        monkeypatch.setattr(app_module.requests, "get", forbidden_network)
        monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
        monkeypatch.setattr(socket.socket, "connect", forbidden_network)

        resp = app_client.get("/api/project_hub")

        assert network_calls == []
        assert transport_calls == [(app_module.config.OLLAMA_TAGS_URL, {"timeout": 1.5})]
        assert resp.status_code == 200
        architecture = {item["id"]: item["status"] for item in resp.get_json()["architecture"]}
        assert architecture["oracle"] == "Down (Unavailable)"
        assert architecture["ollama"] == "Down (Unavailable)"


class TestPublicUiBodyExtraction:
    def test_public_ui_route_bodies_live_in_blueprint_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        public_ui_content = (PROJECT_ROOT / "routes" / "public_ui.py").read_text(encoding="utf-8")

        for signature in ("def index(", "def legacy_index(", "def serve_react_assets("):
            assert signature not in app_content
            assert signature in public_ui_content


class TestReadOnlyBodyExtraction:
    def test_schema_catalog_route_bodies_live_in_schema_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_read_content = (PROJECT_ROOT / "routes" / "schema_read.py").read_text(encoding="utf-8")

        for signature in ("def api_schemas(", "def api_tables("):
            assert signature not in app_content
            assert signature in schema_read_content

    def test_security_audit_read_route_body_lives_in_schema_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_read_content = (PROJECT_ROOT / "routes" / "schema_read.py").read_text(encoding="utf-8")

        assert "def api_security_audit(" not in app_content
        assert "def api_security_audit(" in schema_read_content

    def test_alert_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in ("def api_alerts(", "def api_alerts_history("):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_monitoring_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_monitoring_freshness(",
            "def api_monitoring_tablespace(",
            "def api_monitoring_etl_metrics(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_dba_core_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_dba_sessions(",
            "def api_dba_locks(",
            "def api_dba_tablespace(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_dba_privilege_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_dba_privileges(",
            "def api_dba_object_privileges(",
            "def api_dba_invalid_objects(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_dba_status_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_dba_redo_status(",
            "def api_dba_backup_status(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_dba_sql_metrics_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_dba_top_sql(",
            "def api_dba_wait_events(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_lineage_basic_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_lineage(",
            "def api_lineage_impact(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_lineage_usage_read_route_body_lives_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        assert "def api_lineage_usage(" not in app_content
        assert "def api_lineage_usage(" in ops_read_content

    def test_mapping_snapshot_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_mapping_snapshots(",
            "def api_mapping_compare(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_mapping_state_read_route_bodies_live_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_mapping_runs(",
            "def api_mapping_state(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

    def test_mapping_log_read_route_body_lives_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        assert "def api_mapping_log(" not in app_content
        assert "def api_mapping_log(" in ops_read_content

    def test_mapping_graph_info_read_route_body_lives_in_ops_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")

        assert "def api_mapping_graph_info(" not in app_content
        assert "def api_mapping_graph_info(" in ops_read_content

    def test_odi_metadata_read_route_bodies_live_in_route_modules(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ops_read_content = (PROJECT_ROOT / "routes" / "ops_read.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_odi_scenarios(",
            "def api_odi_mappings(",
            "def api_odi_lineage(",
            "def api_odi_kms(",
        ):
            assert signature not in app_content
            assert signature in ops_read_content

        assert "def api_odi_sessions(" not in app_content
        assert "def api_odi_sessions(" in misc_write_content

    def test_schema_status_read_route_bodies_live_in_schema_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_read_content = (PROJECT_ROOT / "routes" / "schema_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_schema_impact(",
            "def api_schema_drift(",
            "def api_schema_findings(",
            "def api_schema_changes(",
            "def api_watermarks(",
        ):
            assert signature not in app_content
            assert signature in schema_read_content

    def test_schema_findings_report_route_bodies_live_in_schema_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_read_content = (PROJECT_ROOT / "routes" / "schema_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_schema_findings_export(",
            "def api_schema_source_target_findings(",
            "def api_schema_pk_parity(",
        ):
            assert signature not in app_content
            assert signature in schema_read_content

    def test_schema_table_read_route_bodies_live_in_schema_read_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_read_content = (PROJECT_ROOT / "routes" / "schema_read.py").read_text(encoding="utf-8")

        for signature in (
            "def api_schema(",
            "def api_source_schema(",
            "def api_preview(",
            "def api_source_preview(",
            "def api_incremental_preflight(",
        ):
            assert signature not in app_content
            assert signature in schema_read_content


class TestWriteBodyExtraction:
    def test_schema_write_route_bodies_live_in_schema_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_write_content = (PROJECT_ROOT / "routes" / "schema_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_schema_change_preflight(",
            "def api_schema_change_apply_db(",
            "def api_schema_change_sync_odi(",
            "def api_schema_change_reconcile_scenario(",
            "def api_detect_schema_changes(",
            "def api_watermark_delta(",
        ):
            assert signature not in app_content
            assert signature in schema_write_content

    def test_incremental_execute_route_body_lives_in_schema_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_write_content = (PROJECT_ROOT / "routes" / "schema_write.py").read_text(encoding="utf-8")

        assert "def api_incremental_execute(" not in app_content
        assert "def api_incremental_execute(" in schema_write_content

    def test_mapping_utility_write_route_bodies_live_in_mapping_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        mapping_write_content = (PROJECT_ROOT / "routes" / "mapping_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_mapping_snapshot(",
            "def api_mapping_analyze_prompt(",
            "def api_mapping_target_preflight(",
            "def api_mapping_generate(",
            "def api_validate_expression(",
        ):
            assert signature not in app_content
            assert signature in mapping_write_content

    def test_mapping_persistent_write_route_bodies_live_in_mapping_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        mapping_write_content = (PROJECT_ROOT / "routes" / "mapping_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_mapping_update(",
            "def api_mapping_delete(",
            "def api_rollback(",
        ):
            assert signature not in app_content
            assert signature in mapping_write_content

    def test_mapping_run_graph_route_bodies_live_in_mapping_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        mapping_write_content = (PROJECT_ROOT / "routes" / "mapping_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_mapping_run(",
            "def api_mapping_generate_graph(",
        ):
            assert signature not in app_content
            assert signature in mapping_write_content

    def test_alert_write_route_bodies_live_in_misc_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_alerts_acknowledge(",
            "def api_alerts_check(",
        ):
            assert signature not in app_content
            assert signature in misc_write_content

    def test_general_utility_write_route_bodies_live_in_misc_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_execute(",
            "def api_export(",
            "def api_clear(",
        ):
            assert signature not in app_content
            assert signature in misc_write_content

    def test_monitoring_lineage_route_bodies_live_in_misc_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_monitoring_dq_scores(",
            "def api_lineage_refresh(",
        ):
            assert signature not in app_content
            assert signature in misc_write_content

    def test_legacy_airflow_route_bodies_live_in_misc_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_airflow_run_odi(",
            "def api_airflow_run_mapping(",
            "def api_airflow_run_dq(",
        ):
            assert signature not in app_content
            assert signature in misc_write_content

    def test_odi_session_suggestion_route_body_lives_in_misc_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        assert "def api_odi_session_suggestion(" not in app_content
        assert "def api_odi_session_suggestion(" in misc_write_content

    def test_odi_mapping_detail_route_body_lives_in_misc_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        misc_write_content = (PROJECT_ROOT / "routes" / "misc_write.py").read_text(encoding="utf-8")

        assert "def api_odi_mapping_detail(" not in app_content
        assert "def api_odi_mapping_detail(" in misc_write_content

    def test_ai_support_write_route_bodies_live_in_ai_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ai_write_content = (PROJECT_ROOT / "routes" / "ai_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_config_llm(",
            "def api_local_analyst_summarize(",
            "def api_ai_orchestrate(",
            "def api_rag_knowledge(",
        ):
            assert signature not in app_content
            assert signature in ai_write_content

    def test_ai_ask_route_body_lives_in_ai_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ai_write_content = (PROJECT_ROOT / "routes" / "ai_write.py").read_text(encoding="utf-8")

        assert "def api_ask(" not in app_content
        assert "def api_ask(" in ai_write_content

    def test_ai_chat_helper_bodies_live_in_ai_helpers_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        ai_helpers_path = PROJECT_ROOT / "ai_helpers.py"
        assert ai_helpers_path.exists()
        ai_helpers_content = ai_helpers_path.read_text(encoding="utf-8")

        for signature in (
            "def _is_modeled_tables_question(",
            "def _modeled_tables_answer(",
            "def _is_unmapped_odi_flow_question(",
            "def _remembered_unmapped_flow_mapping_answer(",
            "def _table_data_answer(",
            "def _llm_status_answer(",
        ):
            assert signature not in app_content
            assert signature in ai_helpers_content

    def test_schema_change_helper_bodies_live_in_schema_helpers_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        schema_helpers_path = PROJECT_ROOT / "schema_helpers.py"
        assert schema_helpers_path.exists()
        schema_helpers_content = schema_helpers_path.read_text(encoding="utf-8")

        for signature in (
            "def _schema_impact_payload(",
            "def _schema_change_preflight_payload(",
            "def _schema_change_apply_db_payload(",
            "def _schema_change_sync_odi_payload(",
            "def _schema_change_reconcile_scenario_payload(",
        ):
            assert signature not in app_content
            assert signature in schema_helpers_content

    def test_app_utility_health_helper_bodies_live_outside_app_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        app_helpers_path = PROJECT_ROOT / "app_helpers.py"
        health_helpers_path = PROJECT_ROOT / "health_helpers.py"
        odi_helpers_path = PROJECT_ROOT / "odi_helpers.py"
        assert app_helpers_path.exists()
        assert health_helpers_path.exists()
        assert odi_helpers_path.exists()
        app_helpers_content = app_helpers_path.read_text(encoding="utf-8")
        health_helpers_content = health_helpers_path.read_text(encoding="utf-8")
        odi_helpers_content = odi_helpers_path.read_text(encoding="utf-8")

        for signature in ("def df_to_jsonable(", "def _group_odi_mapping_lineage_items("):
            assert signature not in app_content
            assert signature in app_helpers_content
        for signature in ("def _chk_oracle(", "def _chk_local_openai(", "def _chk_ai_orchestrator("):
            assert signature not in app_content
            assert signature in health_helpers_content
        assert "def _read_mapping_km_options(" not in app_content
        assert "def _read_mapping_km_options(" in odi_helpers_content

    def test_quality_write_route_bodies_live_in_quality_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        quality_write_content = (PROJECT_ROOT / "routes" / "quality_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_quality_validate(",
            "def api_quality_reconcile(",
            "def api_quality_autofix_column(",
            "def api_quality_autofix_all_columns(",
            "def api_quality_sync_mapping(",
            "def api_quality_create_err(",
            "def api_quality_create_all_err(",
            "def api_quality_validate_insert(",
            "def api_skey_create(",
            "def api_skey_expression(",
        ):
            assert signature not in app_content
            assert signature in quality_write_content

    def test_auth_write_route_bodies_live_in_auth_write_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        auth_write_content = (PROJECT_ROOT / "routes" / "auth_write.py").read_text(encoding="utf-8")

        for signature in (
            "def api_auth_login(",
            "def api_auth_logout(",
        ):
            assert signature not in app_content
            assert signature in auth_write_content


class TestDisabledIntegrationEndpoints:
    def test_git_endpoints_remain_disabled(self, app_client):
        for path in ["/api/git/status", "/api/git/log"]:
            resp = app_client.get(path)

            assert resp.status_code == 410
            body = resp.get_json()
            assert body["error"] == "Git/Gitea is disabled."
            assert "backup is not used" in body["reason"]


class TestAiToolsEndpoint:
    def test_ai_tools_wraps_registry_without_side_effects(self, app_client, monkeypatch):
        import app as app_module

        monkeypatch.setattr(
            app_module,
            "get_tool_registry",
            lambda: [{"name": "odi.incremental_strategy_preflight", "mode": "read_only"}],
        )

        resp = app_client.get("/api/ai/tools")

        assert resp.status_code == 200
        assert resp.get_json() == {
            "ok": True,
            "tools": [{"name": "odi.incremental_strategy_preflight", "mode": "read_only"}],
        }

    def test_skey_expression_requires_fields(self, app_client):
        resp = app_client.post(
            "/api/skey/expression",
            json={"owner": "DWH", "table": "PRODUCT"},
        )
        assert resp.status_code == 400
