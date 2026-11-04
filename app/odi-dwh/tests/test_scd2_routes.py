from __future__ import annotations

import sys
from pathlib import Path

import pytest

import scd2_builder
import scd2_service
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    make_services,
    plan_catalog_rows,
    probe_payload,
)
from tests.test_remote_run_contract import RemoteFakeOrchestrator as FakeOrchestrator

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.workspace import appsec_gate  # noqa: E402

APP_PY = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


@pytest.fixture
def stub_services(monkeypatch, plan):
    def install(orchestrator=None, **kwargs):
        orchestrator = orchestrator or FakeOrchestrator(plan)
        services = make_services(orchestrator, **kwargs)
        monkeypatch.setattr(scd2_service, "default_services", lambda: services)
        return orchestrator

    return install


def matrix_row(path):
    rows = [r for r in appsec_gate.build_endpoint_matrix(APP_PY) if r["path"] == path]
    assert len(rows) == 1
    return rows[0]


# --------------------------------------------------------------- matrix gates


@pytest.mark.parametrize(
    "path, methods, risk, approval, rate",
    [
        ("/api/scd2/plan", ["POST"], "write", False, "20 per minute"),
        ("/api/scd2/generate", ["POST"], "persistent_write", True, "10 per minute"),
        ("/api/scd2/run", ["POST"], "persistent_write", True, "5 per minute"),
        ("/api/scd2/postflight", ["GET"], "read", False, "30 per minute"),
    ],
)
def test_new_endpoints_are_classified_in_the_appsec_matrix(path, methods, risk, approval, rate):
    row = matrix_row(path)
    assert row["methods"] == methods
    assert row["risk"] == risk
    assert row["approval_required"] is approval
    assert row["rate_limit"] == rate
    assert row["csrf_exempt"] is False


def test_appsec_gate_passes_with_new_endpoints():
    report = appsec_gate.run_gate(APP_PY, APP_PY.with_name("config.py"), APP_PY.parent)
    assert report["passed"] is True, [c for c in report["checks"] if not c["passed"]]


# ------------------------------------------------------------------ route use


def test_plan_route_needs_no_approval(app_client, stub_services):
    stub_services()
    response = app_client.post("/api/scd2/plan", json={**REQUEST, "source_columns": SOURCE_COLUMNS})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "planned" and body["plan"]["target"] == {"owner": "DM", "table": "DIM_CUSTOMER"}


@pytest.mark.parametrize("path", ["/api/scd2/generate", "/api/scd2/run"])
@pytest.mark.parametrize("approved", [None, False, "true", 1, "yes"])
def test_write_routes_require_json_boolean_true(app_client, stub_services, path, approved):
    orchestrator = stub_services()
    body = dict(REQUEST) if path.endswith("generate") else {
        "source_owner": "DWH", "source_table": "ODS_CUSTOMERS", "target_owner": "DM",
        "target_table": "DIM_CUSTOMER", "mapping_name": "MAP_X", "natural_key": ["CUST_ID"],
    }
    if approved is not None:
        body["approved"] = approved
    response = app_client.post(path, json=body)
    assert response.status_code == 400
    assert "approved:true" in response.get_json()["error"]
    assert orchestrator.calls == []


def test_generate_route_end_to_end_with_mocks(app_client, stub_services, plan):
    orchestrator = stub_services(ddl_handler=lambda sql, params: _ddl(plan, sql))
    response = app_client.post("/api/scd2/generate", json={**REQUEST, "approved": True})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "generated"
    assert [s["step"] for s in body["steps"]][0] == "odi_probe"
    # The Groovy approval context was bound by the route for repository writes.
    writes = [c for c in orchestrator.calls if c[0] == "groovy" and c[1] != "scd2_probe.groovy"]
    assert writes and all(c[2] is True for c in writes)


def _ddl(plan, sql):
    if "FROM ALL_USERS" in sql:
        return [(1,)]
    if "FROM ALL_TABLES" in sql or "FROM ALL_SEQUENCES" in sql:
        return [(0,)]
    if "FROM ALL_TAB_COLUMNS" in sql:
        return plan_catalog_rows(plan)
    return []


def test_generate_route_reports_dm_model_missing(app_client, stub_services, plan):
    stub_services(FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(target_model_exists=False)}))
    response = app_client.post("/api/scd2/generate", json={**REQUEST, "approved": True})
    assert response.status_code == 409
    assert response.get_json()["status"] == "blocked_dm_model_missing"


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_owner": "ODS"},
        {"target_owner": "AUDIT_USER"},
        {"target_table": "DIM;DROP"},
        {"mapping_name": "x y"},
        {"natural_key": "CUST_ID"},
    ],
)
def test_generate_route_rejects_bad_owner_and_identifiers(app_client, stub_services, overrides):
    orchestrator = stub_services()
    response = app_client.post("/api/scd2/generate", json={**REQUEST, **overrides, "approved": True})
    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_request"
    assert orchestrator.calls == []


def test_non_object_body_is_treated_as_empty(app_client, stub_services):
    stub_services()
    assert app_client.post("/api/scd2/generate", json=[1, 2]).status_code == 400
    assert app_client.post("/api/scd2/plan", data="not json", content_type="application/json").status_code == 400


def test_run_route_returns_postflight(app_client, stub_services, plan):
    rows = plan_catalog_rows(plan)

    def catalog(sql, params):
        if "FROM ALL_TAB_COLUMNS" in sql:
            return rows
        if "TO_CHAR(MAX" in sql:
            return [("2026-11-04",)]
        return [(3,)]

    stub_services(
        FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe_payload(mapping_exists=True, scenario_exists=True)}),
        catalog_handler=catalog,
    )
    response = app_client.post(
        "/api/scd2/run",
        json={
            "approved": True, "source_owner": "DWH", "source_table": "ODS_CUSTOMERS",
            "target_owner": "DM", "target_table": "DIM_CUSTOMER",
            "mapping_name": "MAP_ODS_CUST_TO_DIM_CUST", "natural_key": ["CUST_ID"],
        },
    )
    body = response.get_json()
    assert response.status_code == 200 and body["status"] == "run_completed"
    assert body["postflight"]["counts"]["target_total"] == 3


def test_postflight_route(app_client, stub_services, plan):
    stub_services(catalog_handler=lambda sql, params: [("2026-11-04",)] if "TO_CHAR" in sql else [(2,)])
    response = app_client.get("/api/scd2/postflight?owner=DM&table=DIM_CUSTOMER&natural_key=CUST_ID")
    assert response.status_code == 200
    assert response.get_json()["counts"]["target_current"] == 2
    assert app_client.get("/api/scd2/postflight?owner=SYS&table=T&natural_key=A").status_code == 400
    assert app_client.get("/api/scd2/postflight").status_code == 400


def test_routes_do_not_leak_exception_text(app_client, monkeypatch):
    def boom():
        raise RuntimeError("password=hunter2 jdbc:oracle:thin:@host")

    monkeypatch.setattr(scd2_service, "default_services", boom)
    response = app_client.post("/api/scd2/plan", json=REQUEST)
    assert response.status_code == 500
    assert "hunter2" not in response.get_data(as_text=True)


# ------------------------------------------------------------------------ CSRF


def test_write_routes_enforce_csrf_when_enabled(stub_services):
    from app import app, limiter

    stub_services()
    old_csrf = app.config.get("WTF_CSRF_ENABLED")
    old_limiter = limiter.enabled
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = True
    limiter.enabled = False
    try:
        with app.test_client() as client:
            for path in ("/api/scd2/plan", "/api/scd2/generate", "/api/scd2/run"):
                response = client.post(path, json={**REQUEST, "approved": True})
                assert response.status_code == 400, path
                assert "CSRF" in response.get_data(as_text=True), path
    finally:
        app.config["WTF_CSRF_ENABLED"] = old_csrf
        limiter.enabled = old_limiter


def test_dm_is_not_in_the_global_owner_allowlist(app_client):
    import app as app_module

    assert app_module._validate_owner("DM") is False
    assert app_module._validate_owner("SYS") is False
    response = app_client.post(
        "/api/mapping/delete", json={"mapping_name": "MAP_X", "tgt_owner": "DM", "approved": True}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid target owner."


def test_postflight_invalid_owner_and_table_are_not_echoed(app_client, stub_services):
    stub_services()
    response = app_client.get("/api/scd2/postflight?owner=EVIL_OWNER_X&table=EVIL_TABLE_Y&natural_key=A")
    assert response.status_code == 400
    text = response.get_data(as_text=True)
    assert "EVIL_OWNER_X" not in text and "EVIL_TABLE_Y" not in text
    response = app_client.get("/api/scd2/postflight?owner=DM&table=BAD%20TABLE%3B&natural_key=A")
    assert response.status_code == 400 and "BAD" not in response.get_data(as_text=True)
