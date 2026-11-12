from __future__ import annotations

import json
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from route_inventory import build_route_contract, route_contract_rows, write_route_contract
from tools.workspace import appsec_gate


ACTIVE_APP_ROOT = WORKSPACE_ROOT / "app" / "odi-dwh"
PHASE0_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "route_contract_phase0_20260702.json"
ROUTE_CONTRACT_FIELDS = (
    "path",
    "methods",
    "function",
    "risk",
    "approval_required",
    "rate_limit",
    "csrf_exempt",
)
SIMPLE_GET_BLUEPRINT_PATHS = (
    "/",
    "/legacy",
    "/assets/<path:path>",
    "/api/security/audit",
    "/api/schemas",
    "/api/tables",
    "/api/schema/impact",
    "/api/schema/drift",
    "/api/schema/findings",
    "/api/schema/findings/export",
    "/api/schema/source-target/findings",
    "/api/schema/pk-parity",
    "/api/schema/<path:table_name>",
    "/api/source/schema/<path:table_name>",
    "/api/mssql/schema/<path:table_name>",
    "/api/preview/<path:table_name>",
    "/api/source/preview/<path:table_name>",
    "/api/schema-changes",
    "/api/watermarks",
    "/api/incremental/preflight",
    "/api/monitoring/freshness",
    "/api/monitoring/tablespace",
    "/api/monitoring/etl-metrics",
    "/api/dba/sessions",
    "/api/dba/locks",
    "/api/dba/tablespace",
    "/api/dba/privileges",
    "/api/dba/object-privileges",
    "/api/dba/invalid-objects",
    "/api/dba/redo-status",
    "/api/dba/backup-status",
    "/api/dba/top-sql",
    "/api/dba/wait-events",
    "/api/lineage/<table_name>",
    "/api/lineage/impact/<source_table>",
    "/api/lineage/usage/<table_name>",
    "/api/mapping/snapshots/<mapping_name>",
    "/api/mapping/compare/<mapping_name>",
    "/api/mapping/graph-info",
    "/api/mapping/runs",
    "/api/mapping/state/<mapping_name>",
    "/api/mapping-log/<batch_id>",
    "/api/alerts",
    "/api/alerts/history",
    "/api/odi/scenarios",
    "/api/odi/mappings",
    "/api/odi/lineage",
    "/api/odi/kms",
)
REMAINING_BLUEPRINT_PATHS = (
    "/api/auth/login",
    "/api/auth/logout",
    "/api/config/llm",
    "/api/local-analyst/summarize",
    "/api/ai/orchestrate",
    "/api/rag_knowledge",
    "/api/schema/change/preflight",
    "/api/schema/change/apply-db",
    "/api/schema/change/sync-odi",
    "/api/schema/change/reconcile-scenario",
    "/api/ask",
    "/api/execute",
    "/api/export",
    "/api/clear",
    "/api/schema-changes/detect",
    "/api/watermarks/delta",
    "/api/incremental/execute",
    "/api/quality/validate",
    "/api/quality/reconcile",
    "/api/quality/autofix-column",
    "/api/quality/autofix-all-columns",
    "/api/quality/sync-mapping",
    "/api/quality/create-error-table",
    "/api/quality/create-all-error-tables",
    "/api/quality/validate-insert",
    "/api/monitoring/dq-scores",
    "/api/skey/create",
    "/api/skey/expression",
    "/api/lineage/refresh",
    "/api/mapping/snapshot",
    "/api/mapping/analyze-prompt",
    "/api/mapping/target-preflight",
    "/api/mapping/generate",
    "/api/mapping/validate-expression",
    "/api/mapping/update",
    "/api/mapping/delete",
    "/api/mapping/run",
    "/api/mapping/generate-graph",
    "/api/rollback/<batch_id>",
    "/api/alerts/acknowledge",
    "/api/alerts/check",
    "/api/odi/sessions",
    "/api/odi/session_suggestion",
    "/api/odi/mapping_detail",
    "/api/airflow/run-odi",
    "/api/airflow/run-mapping",
    "/api/airflow/run-dq",
)


def _stable_route_contract(contract):
    return {
        "source": contract["source"],
        "route_count": contract["route_count"],
        "routes": [
            {field: row[field] for field in ROUTE_CONTRACT_FIELDS}
            for row in contract["routes"]
        ],
    }


def test_runtime_route_inventory_matches_static_appsec_matrix():
    from app import app

    static_rows = appsec_gate.build_endpoint_matrix(ACTIVE_APP_ROOT / "app.py")
    runtime_rows = route_contract_rows(app)

    assert runtime_rows == static_rows


def test_route_contract_keeps_critical_write_gate_metadata():
    from app import app

    contract = build_route_contract(app, ACTIVE_APP_ROOT / "app.py")

    incremental_execute = next(
        row
        for row in contract["routes"]
        if row["path"] == "/api/incremental/execute" and "POST" in row["methods"]
    )
    assert incremental_execute["risk"] == "persistent_write"
    assert incremental_execute["approval_required"] is True
    assert incremental_execute["rate_limit"] == "5 per minute"

    mapping_run = next(
        row
        for row in contract["routes"]
        if row["path"] == "/api/mapping/run" and "POST" in row["methods"]
    )
    assert mapping_run["risk"] == "persistent_write"
    assert mapping_run["approval_required"] is True
    assert mapping_run["rate_limit"] == "5 per minute"

    quality_sync = next(
        row
        for row in contract["routes"]
        if row["path"] == "/api/quality/sync-mapping" and "POST" in row["methods"]
    )
    assert quality_sync["risk"] == "persistent_write"
    assert quality_sync["approval_required"] is True
    assert quality_sync["rate_limit"] == "5 per minute"

    assert contract["route_count"] == len(contract["routes"])


def test_route_contract_can_be_written_as_metadata_only_json(tmp_path):
    from app import app

    output_path = tmp_path / "route_contract.json"
    write_route_contract(output_path, app, ACTIVE_APP_ROOT / "app.py")

    contract = json.loads(output_path.read_text(encoding="utf-8"))
    assert contract["source"] == "flask_url_map_plus_appsec_endpoint_matrix"
    assert contract["route_count"] == len(contract["routes"])
    assert any(
        row["path"] == "/api/incremental/execute"
        and row["methods"] == ["POST"]
        and row["rate_limit"] == "5 per minute"
        for row in contract["routes"]
    )


def test_route_contract_matches_reviewed_phase0_fixture():
    from app import app

    expected = json.loads(PHASE0_FIXTURE.read_text(encoding="utf-8"))
    actual = build_route_contract(app, ACTIVE_APP_ROOT / "app.py")

    assert _stable_route_contract(actual) == _stable_route_contract(expected)


def test_remaining_read_only_routes_are_registered_by_blueprints():
    app_py = (ACTIVE_APP_ROOT / "app.py").read_text(encoding="utf-8")
    route_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ACTIVE_APP_ROOT / "routes").glob("*.py")
    )

    for route_path in SIMPLE_GET_BLUEPRINT_PATHS:
        assert f'@app.route("{route_path}")' not in app_py
        assert f'@bp.route("{route_path}")' in route_sources


def test_remaining_write_and_rate_limited_routes_are_registered_by_blueprints():
    app_py = (ACTIVE_APP_ROOT / "app.py").read_text(encoding="utf-8")
    route_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ACTIVE_APP_ROOT / "routes").glob("*.py")
    )

    for route_path in REMAINING_BLUEPRINT_PATHS:
        assert f'@app.route("{route_path}"' not in app_py
        assert f'@bp.route("{route_path}"' in route_sources


def test_route_modules_use_registered_app_module_dependency():
    import app as app_module
    from routes.deps import get_app_module

    route_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ACTIVE_APP_ROOT / "routes").glob("*.py")
    )

    assert get_app_module() is app_module
    assert "import app as app_module" not in route_sources
