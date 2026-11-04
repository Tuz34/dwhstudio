from __future__ import annotations

import os
import subprocess

import requests

import config
from db import test_connection

def _chk_oracle():
    try: return test_connection()
    except Exception: return False

def _chk_source():
    """Checks access to the active source (SOURCE_DB_TYPE: oracle|mssql)."""
    try:
        from source_connector import get_source_connector
        return len(get_source_connector().get_tables()) > 0
    except Exception:
        return False

# Backward compatibility: the old "mssql" check now delegates to the source connector.
def _chk_mssql():
    return _chk_source()

def _chk_rag():
    try:
        from rag_manager import _load_db
        return _load_db() is not None
    except Exception: return False

def _chk_llm():
    """Checks Ollama (local LLM), which is needed for embeddings."""
    try:
        res = requests.get(config.OLLAMA_TAGS_URL, timeout=4)
        return res.status_code == 200
    except Exception: return False

def _chk_claude():
    """Checks the Claude API (Anthropic), the main engine for AI Agent."""
    try:
        key = os.getenv("ANTHROPIC_API_KEY") or getattr(config, "ANTHROPIC_API_KEY", "")
        return bool(key and len(key) > 10)
    except Exception: return False

def _chk_local_openai():
    try:
        base_url = (os.getenv("LOCAL_OPENAI_BASE_URL") or config.LOCAL_OPENAI_BASE_URL).rstrip("/")
        headers = {}
        api_key = os.getenv("LOCAL_OPENAI_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        res = requests.get(f"{base_url}/models", headers=headers, timeout=2)
        return res.status_code == 200
    except Exception: return False

def _chk_docker():
    try:
        import subprocess
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception: return False

def _chk_mcp():
    try:
        from odi_mcp import odi_get_recent_sessions  # noqa: F401
        return True
    except Exception: return False

def _chk_monitoring():
    try:
        from monitoring_mcp import mon_data_freshness  # noqa: F401
        return True
    except Exception: return False

def _chk_dba():
    try:
        from dba_mcp import dba_active_sessions  # noqa: F401
        return True
    except Exception: return False


def _chk_graph_health():
    try:
        from mapping_graph import get_graph
        get_graph()
        return True
    except Exception: return False

def _chk_ai_orchestrator():
    try:
        from ai_orchestrator import get_orchestrator_graph
        get_orchestrator_graph()
        return True
    except Exception: return False

