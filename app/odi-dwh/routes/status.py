from __future__ import annotations

import os

from flask import Blueprint, jsonify

from status_probe_cache import StatusProbeCache

bp = Blueprint("status", __name__)
_PROBE_CACHE = StatusProbeCache()


@bp.route("/api/status")
def api_status():
    """Return global system health while keeping checks read-only."""
    from routes.deps import get_app_module
    app_module = get_app_module()
    from ai_agent import normalize_provider

    checkers = {
        "oracle": app_module._chk_oracle,
        "source": app_module._chk_source,
        "mssql": app_module._chk_mssql,
        "rag": app_module._chk_rag,
        "llm": app_module._chk_llm,
        "local_openai": app_module._chk_local_openai,
        "claude": app_module._chk_claude,
        "docker": app_module._chk_docker,
        "mcp": app_module._chk_mcp,
        "monitoring": app_module._chk_monitoring,
        "dba": app_module._chk_dba,
        "langgraph": app_module._chk_graph_health,
        "ai_orchestrator": app_module._chk_ai_orchestrator,
    }
    checks, retry_after = _PROBE_CACHE.read(checkers)
    if checks is None:
        return jsonify({"error": "Status request limit exceeded."}), 429, {
            "Retry-After": str(retry_after), "Cache-Control": "no-store",
        }

    all_ok = all(checks.values())
    return jsonify({
        "ok": all_ok,
        "checks": checks,
        "count": f"{sum(1 for value in checks.values() if value)}/{len(checks)}",
        "llm_provider": normalize_provider(os.getenv("LLM_PROVIDER", "hybrid")),
    }), 200, {"Cache-Control": "no-store"}
