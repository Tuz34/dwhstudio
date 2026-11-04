from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from werkzeug.security import generate_password_hash


def _enable_auth(monkeypatch, app_module, *, role="operator", audit_path: Path | None = None):
    monkeypatch.setattr(app_module, "APP_AUTH_ENABLED", True)
    monkeypatch.setattr(app_module, "APP_AUTH_USERNAME", "local_admin")
    monkeypatch.setattr(app_module, "APP_AUTH_PASSWORD_HASH", generate_password_hash("correct-password"))
    monkeypatch.setattr(app_module, "APP_AUTH_DEFAULT_ROLE", role)
    if audit_path is not None:
        monkeypatch.setattr(app_module, "SECURITY_AUDIT_LOG_FILE", str(audit_path))


def test_auth_disabled_keeps_existing_local_api_access(app_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "APP_AUTH_ENABLED", False)

    resp = app_client.get("/api/status")

    assert resp.status_code == 200


def test_auth_enabled_blocks_unauthenticated_api_but_allows_status(app_client, monkeypatch):
    import app as app_module

    _enable_auth(monkeypatch, app_module)

    status = app_client.get("/api/status")
    blocked = app_client.get("/api/tables")

    assert status.status_code == 200
    assert blocked.status_code == 401
    assert blocked.get_json()["error"] == "Authentication required."


def test_login_sets_session_and_logout_clears_it(app_client, monkeypatch):
    import app as app_module

    _enable_auth(monkeypatch, app_module, role="admin")

    login = app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "correct-password"},
    )
    status_after_login = app_client.get("/api/auth/status")
    logout = app_client.post("/api/auth/logout")
    status_after_logout = app_client.get("/api/auth/status")

    assert login.status_code == 200
    assert login.get_json()["authenticated"] is True
    assert login.get_json()["user"]["role"] == "admin"
    assert status_after_login.get_json()["authenticated"] is True
    assert logout.status_code == 200
    assert status_after_logout.get_json()["authenticated"] is False


def test_auth_session_uses_configured_timeout():
    import app as app_module

    assert app_module.app.permanent_session_lifetime.total_seconds() > 0
    assert "APP_SESSION_TIMEOUT_MINUTES" in (Path(app_module.app.root_path) / "config.py").read_text(encoding="utf-8")


def test_session_cookie_policy_is_explicit_and_local_safe():
    import app as app_module

    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app_module.app.config["SESSION_COOKIE_SECURE"] is bool(
        app_module.APP_TLS_ENABLED or app_module.APP_REVERSE_PROXY_TLS_ENABLED
    )


def test_viewer_role_cannot_call_write_endpoint(app_client, monkeypatch):
    import app as app_module

    _enable_auth(monkeypatch, app_module, role="viewer")
    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "correct-password"},
    )

    resp = app_client.post("/api/execute", json={"sql": "SELECT 1 FROM DUAL"})

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "Role is not allowed for this operation."


def test_operator_can_reach_write_endpoint_security_contract(app_client, monkeypatch):
    import app as app_module

    _enable_auth(monkeypatch, app_module, role="operator")
    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "correct-password"},
    )
    monkeypatch.setattr(
        app_module,
        "execute_dml",
        lambda *_args, **_kwargs: "OK",
    )

    resp = app_client.post(
        "/api/execute",
        json={"sql": "UPDATE DWH.ODS_HR_EMPLOYEES SET FIRST_NAME = FIRST_NAME", "approved": True},
    )

    assert resp.status_code == 200
    assert resp.get_json()["result"] == "OK"


def test_audit_log_records_redacted_auth_and_denial_events(app_client, monkeypatch):
    import app as app_module

    audit_path = Path(app_module.app.root_path) / ".tmp_auth_audit_test.jsonl"
    if audit_path.exists():
        audit_path.write_text("", encoding="utf-8")
    _enable_auth(monkeypatch, app_module, role="viewer", audit_path=audit_path)

    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "wrong-password"},
    )
    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "correct-password"},
    )
    app_client.post("/api/execute", json={"sql": "DROP TABLE DWH.SECRET_TABLE", "approved": True})

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    actions = {event["action"] for event in events}
    payload = json.dumps(events)

    assert {"login_failed", "login_success", "role_denied"} <= actions
    assert "wrong-password" not in payload
    assert "correct-password" not in payload
    assert "DROP TABLE" not in payload
    audit_path.write_text("# sanitized temporary test artifact\n", encoding="utf-8")


def test_audit_log_events_have_verifiable_hash_chain(app_client, monkeypatch):
    import app as app_module

    audit_path = Path(app_module.app.root_path) / ".tmp_auth_audit_hash_test.jsonl"
    if audit_path.exists():
        audit_path.write_text("", encoding="utf-8")
    _enable_auth(monkeypatch, app_module, role="admin", audit_path=audit_path)

    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "wrong-password"},
    )
    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "correct-password"},
    )

    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert len(events) >= 2
    assert all(event.get("event_hash") for event in events)
    assert events[0]["previous_hash"] == "GENESIS"
    assert events[1]["previous_hash"] == events[0]["event_hash"]
    audit_path.write_text("# sanitized temporary test artifact\n", encoding="utf-8")


def test_admin_can_view_audit_events_and_viewer_cannot(app_client, monkeypatch):
    import app as app_module

    audit_path = Path(app_module.app.root_path) / ".tmp_auth_audit_viewer_test.jsonl"
    if audit_path.exists():
        audit_path.write_text("", encoding="utf-8")
    _enable_auth(monkeypatch, app_module, role="viewer", audit_path=audit_path)
    app_client.post(
        "/api/auth/login",
        json={"username": "local_admin", "password": "correct-password"},
    )

    viewer_resp = app_client.get("/api/security/audit")
    assert viewer_resp.status_code == 403

    with app_client.session_transaction() as sess:
        sess["auth_user"] = {"username": "local_admin", "role": "admin"}

    admin_resp = app_client.get("/api/security/audit")
    payload = admin_resp.get_json()

    assert admin_resp.status_code == 200
    assert payload["chain_valid"] is True
    assert payload["events"]
    assert all("event_hash" in event for event in payload["events"])
    assert "path" not in payload
    audit_path.write_text("# sanitized temporary test artifact\n", encoding="utf-8")


def test_concurrent_audit_events_keep_a_valid_hash_chain(tmp_path, monkeypatch):
    import app as app_module

    audit_path = tmp_path / "concurrent_audit.jsonl"
    monkeypatch.setattr(app_module, "SECURITY_AUDIT_LOG_FILE", str(audit_path))
    original_last_hash = app_module._last_security_audit_hash

    def slow_last_hash(path):
        value = original_last_hash(path)
        time.sleep(0.01)
        return value

    monkeypatch.setattr(app_module, "_last_security_audit_hash", slow_last_hash)
    start = threading.Barrier(8)

    def write_event(index):
        start.wait()
        with app_module.app.test_request_context(f"/api/concurrent/{index}"):
            app_module._record_security_event("concurrent_test", "allowed", index=index)

    threads = [threading.Thread(target=write_event, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    events, chain_valid = app_module._read_security_audit_events(limit=20)
    assert all(not thread.is_alive() for thread in threads)
    assert len(events) == 8
    assert chain_valid is True
