import io
import csv
import hashlib
import math
import os
import re
import secrets
import logging
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pandas as pd
import requests
from flask import Flask, jsonify, request, send_file, session, has_request_context
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash

from config import (
    APP_HOST,
    APP_PORT,
    APP_DEBUG,
    APP_USE_RELOADER,
    DEPLOYMENT_PROFILE,
    APP_TLS_ENABLED,
    APP_REVERSE_PROXY_TLS_ENABLED,
    APP_IP_ALLOWLIST,
    APP_ALLOWED_ORIGINS,
    APP_AUTH_ENABLED,
    APP_AUTH_USERNAME,
    APP_AUTH_PASSWORD_HASH,
    APP_AUTH_DEFAULT_ROLE,
    APP_SESSION_TIMEOUT_MINUTES,
    FLASK_SECRET_KEY,
    SECURITY_AUDIT_LOG_FILE,
    ORACLE_DSN,
    ENVIRONMENT,
)
from db import (
    execute_dml, execute_select,
    get_schema_for_ai, get_table_schema, get_user_tables, get_mssql_tables,
    get_source_tables, get_source_table_schema,
    test_connection,
)
import config
from ai_agent import ask_ai
from ai_orchestrator import get_tool_registry, orchestrate
from schema_change_detector import (
    build_schema_sync_findings,
    build_source_target_metadata_findings,
    detect_changes,
    detect_db_odi_type_drift,
    get_recent_changes,
)
from pk_parity import build_pk_parity_report
from watermark_manager import (
    WatermarkManager,
    build_incremental_execution_request,
    default_incremental_strategy_specs,
    execute_incremental_append,
    run_configured_incremental_strategy_preflight,
)
from data_quality import DataQualityManager
import json
from odi_mcp import odi_get_recent_sessions, odi_get_scenario_list, odi_get_knowledge_modules, odi_get_mapping_lineage, odi_get_mapping_details
from monitoring_mcp import (
    mon_etl_job_metrics, mon_data_freshness, mon_tablespace_usage,
    mon_dq_score, mon_schema_changes, mon_alert_summary,
)
from dba_mcp import (
    dba_active_sessions, dba_blocking_locks, dba_tablespace_detail,
    dba_user_privileges, dba_object_privileges, dba_invalid_objects,
    dba_redo_archive_status, dba_rman_backup_status,
    dba_top_sql, dba_wait_events,
)
from alert_manager import AlertManager
from skey_manager import SkeyManager
from rollback_manager import RollbackManager
from lineage_manager import LineageManager
from mapping_snapshot_manager import MappingSnapshotManager
from mapping_generator import MappingGenerator
from mapping_graph import run_mapping_graph, get_graph, MappingState
from mapping_state_manager import MappingStateManager, redact_sensitive_text
from control_tower_evidence import build_control_tower_evidence
from functools import wraps
from exceptions import (
    DWHBaseError, DatabaseConnectionError, DatabaseQueryError,
    InvalidOwnerError, TableNotFoundError, SchemaValidationError,
)
from deployment_security import validate_deployment_security
from routes.error_responses import redacted_error

PK_PARITY_MAX_KEYS = 100000

logger = logging.getLogger("dwh_app")
_SECURITY_AUDIT_LOCK = threading.Lock()

app = Flask(__name__)

_WEAK_FLASK_SECRET_KEYS = {
    "replace_me", "change_me", "changeme", "secret", "password", "dev", "test", "default",
}


def _configure_flask_secret_key(flask_app: Flask, configured_key: str | None) -> bool:
    """Use a configured signing key only when it is neither weak nor a placeholder."""
    candidate = configured_key if isinstance(configured_key, str) else ""
    normalized = candidate.strip()
    normalized_key = normalized.casefold()
    configured = len(normalized) >= 16 and not any(
        normalized_key.startswith(token) for token in _WEAK_FLASK_SECRET_KEYS
    )
    if configured:
        flask_app.secret_key = candidate
    else:
        flask_app.secret_key = secrets.token_hex(32)
        logger.warning("FLASK_SECRET_KEY is missing or weak; generated a temporary runtime key.")
    return configured


_configured_secret_key = _configure_flask_secret_key(app, FLASK_SECRET_KEY)
app.permanent_session_lifetime = timedelta(minutes=APP_SESSION_TIMEOUT_MINUTES)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(APP_TLS_ENABLED or APP_REVERSE_PROXY_TLS_ENABLED),
)

_DEPLOYMENT_SECURITY_PROFILE = validate_deployment_security({
    "environment": ENVIRONMENT,
    "deployment_profile": DEPLOYMENT_PROFILE,
    "app_host": APP_HOST,
    "app_debug": APP_DEBUG,
    "auth_enabled": APP_AUTH_ENABLED,
    "auth_configured": bool(APP_AUTH_USERNAME and APP_AUTH_PASSWORD_HASH),
    "flask_secret_key_configured": _configured_secret_key,
    "tls_enabled": APP_TLS_ENABLED,
    "reverse_proxy_tls_enabled": APP_REVERSE_PROXY_TLS_ENABLED,
    "ip_allowlist": APP_IP_ALLOWLIST,
    "allowed_origins": APP_ALLOWED_ORIGINS,
})

# S-10: Use the same allowed owner/schema list across all endpoints.
# Include target schemas and active Oracle source schemas (SOURCE_ORACLE_SCHEMAS).
_ALLOWED_OWNERS = {"DWH", "ODS", "AUDIT_USER", "DWH_FLAT", (config.AUDIT_ORACLE_USER or "").upper()}
if (config.SOURCE_DB_TYPE or "").lower() == "oracle":
    _ALLOWED_OWNERS |= set(config.SOURCE_ORACLE_SCHEMAS or [])

_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}
_PUBLIC_API_PATHS = {
    "/api/status",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/deployment/security-profile",
}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

def _validate_owner(owner: str) -> bool:
    return owner.upper() in _ALLOWED_OWNERS


def _valid_identifier(value: str) -> bool:
    return bool(_IDENTIFIER_RE.match((value or "").strip().upper()))


def _valid_version(value: str) -> bool:
    return bool(_VERSION_RE.match((value or "").strip()))


def _approval_error(body: dict, action: str):
    if body.get("approved") is not True:
        return jsonify({"error": f"approved:true is required for {action}."}), 400
    return None


def _validate_incremental_spec_name(value: str | None) -> str:
    spec_name = str(value or "SH_SALES_ODS").strip().upper()
    if spec_name not in default_incremental_strategy_specs():
        raise ValueError("Unknown incremental spec.")
    return spec_name


def _open_incremental_target_connection():
    import oracledb

    return oracledb.connect(
        user=config.TARGET_ORACLE_USER,
        password=config.TARGET_ORACLE_PASSWORD,
        dsn=config.TARGET_ORACLE_DSN,
    )


def require_valid_owner(param: str = "owner"):
    """Read owner from the request body or arguments and validate it against the allowlist.
    Return HTTP 400 for an invalid owner; otherwise pass it to the view as an owner keyword argument.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            body = request.get_json(silent=True) or {}
            owner = (body.get(param) or request.args.get(param) or "DWH").upper()
            if not _validate_owner(owner):
                raise InvalidOwnerError(f"Invalid owner: {owner}")
            kwargs["owner"] = owner
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# O2: Require the X-CSRFToken header for CSRF protection on POST and DELETE requests.
app.config['WTF_CSRF_TIME_LIMIT']  = 3600          # Token valid for one hour
app.config['WTF_CSRF_HEADERS']     = ['X-CSRFToken', 'X-CSRF-Token']
csrf = CSRFProtect(app)

# O3: Limit /api/ask to 20 requests per minute to control LLM costs.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],           # No global limit; decorators control individual routes
    storage_uri="memory://",
)


def _auth_enabled() -> bool:
    return bool(APP_AUTH_ENABLED)


def _auth_configured() -> bool:
    return bool(APP_AUTH_USERNAME and APP_AUTH_PASSWORD_HASH)


def _current_user() -> dict | None:
    user = session.get("auth_user")
    if isinstance(user, dict) and user.get("username"):
        role = str(user.get("role") or "viewer").lower()
        return {"username": str(user["username"]), "role": role if role in _ROLE_ORDER else "viewer"}
    return None


def _has_role(required_role: str) -> bool:
    user = _current_user()
    if not user:
        return False
    return _ROLE_ORDER.get(user["role"], 0) >= _ROLE_ORDER[required_role]


def _auth_public_path(path: str) -> bool:
    return path in _PUBLIC_API_PATHS or path.startswith("/assets/")


def _audit_redact(value):
    if isinstance(value, dict):
        return {
            str(key): ("<redacted>" if re.search(r"password|passwd|pwd|secret|token|api[_-]?key|sql", str(key), re.I)
                       else _audit_redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_audit_redact(item) for item in value[:20]]
    if isinstance(value, str):
        text = redact_sensitive_text(value)
        text = re.sub(r"(?is)\b(drop|alter|truncate|delete|update|insert|merge|create)\b.+", "<redacted_sql>", text)
        return text[:240]
    return value


def _record_security_event(action: str, outcome: str, **details):
    event = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "action": action,
        "outcome": outcome,
        "path": request.path if has_request_context() else None,
        "method": request.method if has_request_context() else None,
        "actor": (_current_user() or {}).get("username", "anonymous"),
        "role": (_current_user() or {}).get("role"),
        "correlation_id": request.headers.get("X-Request-ID") if has_request_context() else None,
        "details": _audit_redact(details),
    }
    try:
        audit_path = _security_audit_path()
        with _SECURITY_AUDIT_LOCK:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            event["previous_hash"] = _last_security_audit_hash(audit_path)
            event["event_hash"] = _security_event_hash(event)
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        logger.warning("Security audit event could not be written: %s", type(exc).__name__)


def _security_audit_path() -> Path:
    audit_path = Path(SECURITY_AUDIT_LOG_FILE)
    if not audit_path.is_absolute():
        audit_path = Path(app.root_path) / audit_path
    return audit_path


def _security_event_hash(event: dict) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_security_audit_hash(audit_path: Path) -> str:
    if not audit_path.exists():
        return "GENESIS"
    try:
        for line in reversed(audit_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip() or line.startswith("#"):
                continue
            event = json.loads(line)
            return str(event.get("event_hash") or "GENESIS")
    except Exception:
        return "GENESIS"
    return "GENESIS"


def _read_security_audit_events(limit: int = 100) -> tuple[list[dict], bool]:
    audit_path = _security_audit_path()
    with _SECURITY_AUDIT_LOCK:
        if not audit_path.exists():
            return [], True
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    events: list[dict] = []
    chain_valid = True
    previous = "GENESIS"
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            chain_valid = False
            continue
        expected_hash = _security_event_hash(event)
        if event.get("previous_hash") != previous or event.get("event_hash") != expected_hash:
            chain_valid = False
        previous = str(event.get("event_hash") or previous)
        events.append(event)
    return events[-limit:], chain_valid


@app.before_request
def enforce_local_auth():
    if not _auth_enabled() or not request.path.startswith("/api/") or _auth_public_path(request.path):
        return None
    if not _auth_configured():
        _record_security_event("auth_unconfigured", "blocked")
        return jsonify({"error": "Authentication is enabled but not configured."}), 503
    if not _current_user():
        _record_security_event("auth_required", "blocked")
        return jsonify({"error": "Authentication required."}), 401
    if request.method in _WRITE_METHODS and not _has_role("operator"):
        _record_security_event("role_denied", "blocked", required_role="operator")
        return jsonify({"error": "Role is not allowed for this operation."}), 403
    return None


@app.after_request
def audit_security_sensitive_response(response):
    if _auth_enabled() and request.path.startswith("/api/") and request.method in _WRITE_METHODS:
        if not _auth_public_path(request.path):
            _record_security_event(
                "api_write",
                "allowed" if response.status_code < 400 else "failed",
                status_code=response.status_code,
            )
    return response

# --- Central error handlers ---
@app.errorhandler(InvalidOwnerError)
def handle_invalid_owner(e: InvalidOwnerError):
    return jsonify({"error": "Invalid owner."}), 400

@app.errorhandler(TableNotFoundError)
def handle_table_not_found(e: TableNotFoundError):
    return jsonify({"error": "Table not found."}), 404

@app.errorhandler(DatabaseConnectionError)
def handle_db_connection(e: DatabaseConnectionError):
    return redacted_error("Database connection", e, status=503)

@app.errorhandler(DatabaseQueryError)
def handle_db_query(e: DatabaseQueryError):
    return redacted_error("Database query", e)

@app.errorhandler(SchemaValidationError)
def handle_schema_validation(e: SchemaValidationError):
    return redacted_error("Schema validation", e, status=422)

@app.errorhandler(DWHBaseError)
def handle_dwh_base(e: DWHBaseError):
    return redacted_error("Application operation", e)

# --- Shared helpers and fallback error handling ---

ORACLE_DSN_DISPLAY = (
    ORACLE_DSN.split(":")[0] + "/" + ORACLE_DSN.split("/")[-1]
    if ORACLE_DSN else "DWH"
)


from app_helpers import df_to_jsonable, _group_odi_mapping_lineage_items

from ai_helpers import (
    _build_table_preview_sql,
    _extract_context_target_table,
    _extract_mapping_name_from_text,
    _extract_source_schema_for_flow_inventory,
    _extract_table_ref_from_text,
    _flow_row_to_mapping_memory,
    _has_recent_bulk_model_intent,
    _is_llm_status_question,
    _is_modeled_tables_question,
    _is_pronoun_bulk_model_intent,
    _is_remembered_unmapped_flow_mapping_request,
    _is_table_data_request,
    _is_truncate_mapping_intent,
    _is_unmapped_odi_flow_question,
    _llm_status_answer,
    _load_unmapped_odi_flow_memory,
    _local_openai_model_ids,
    _mapping_name_for_ods_flow,
    _mapping_request_from_memory,
    _modeled_tables_answer,
    _modeled_target_lookup,
    _normalize_chat_text,
    _ollama_model_names,
    _recent_chat_text,
    _remember_modeled_target,
    _remember_unmapped_odi_flows,
    _remembered_unmapped_flow_mapping_answer,
    _unmapped_odi_flow_answer,
    _table_availability_answer,
    _table_data_answer,
    _table_data_clarification_answer,
    _truncate_mapping_intent_answer,
)

@app.errorhandler(404)
@app.errorhandler(500)
def catch_all(e):
    status = int(getattr(e, "code", 500) or 500)
    if status >= 500:
        original_error = getattr(e, "original_exception", None) or e
        error_type = type(original_error).__name__
        logger.error("Unhandled request failed: %s", error_type)
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "Internal server error.",
                "error_type": error_type,
            }), 500
        return "Internal server error.", 500

    # Unknown non-API paths fall back to the React application for client-side routing.
    if request.path.startswith("/api/"):
        return jsonify({"error": "Endpoint not found."}), 404
    if request.path.startswith("/static/") or request.path.startswith("/assets/"):
        return "Static asset not found.", 404
    from routes.public_ui import render_react_index
    return render_react_index()


# --- Connection API ---

# C-2: Run status checks in parallel with ThreadPoolExecutor.
from health_helpers import (
    _chk_ai_orchestrator,
    _chk_claude,
    _chk_dba,
    _chk_docker,
    _chk_graph_health,
    _chk_llm,
    _chk_local_openai,
    _chk_mcp,
    _chk_monitoring,
    _chk_mssql,
    _chk_oracle,
    _chk_rag,
    _chk_source,
)

from schema_helpers import (
    _classify_type_change,
    _column_after,
    _get_db_column_definitions,
    _get_object_grants,
    _max_text_length,
    _normalize_column_row,
    _normalize_sql_type,
    _parse_schema_object_ref,
    _parse_sql_type_parts,
    _parse_varchar_length,
    _quote_ident,
    _schema_change_apply_db_payload,
    _schema_change_preflight_payload,
    _schema_change_reconcile_scenario_payload,
    _schema_change_sync_odi_payload,
    _schema_impact_payload,
)

from odi_helpers import _read_mapping_km_options

from routes import register_blueprints
from routes.deps import set_app_module


set_app_module(sys.modules[__name__])
register_blueprints(app)


if __name__ == "__main__":
    app.run(host=APP_HOST, debug=APP_DEBUG, port=APP_PORT, use_reloader=APP_USE_RELOADER)


