from pathlib import Path

from init_audit_infra import AUDIT_SCHEMA_OWNER, initialize_audit_infra


def test_initialize_audit_infra_blocks_without_password(monkeypatch):
    monkeypatch.delenv("SETUP_AUDIT_USER_PASSWORD", raising=False)
    monkeypatch.delenv("AUDIT_USER_PASSWORD", raising=False)

    result = initialize_audit_infra(load_env=False)

    assert result["status"] == "blocked_missing_password"
    assert result["audit_schema_owner"] == "AUDIT_USER"
    assert result["secret_printed"] is False
    assert result["operations"] == []


def test_initialize_audit_infra_dry_run_does_not_connect(monkeypatch):
    monkeypatch.setenv("SETUP_AUDIT_USER_PASSWORD", "not-used-in-dry-run")

    result = initialize_audit_infra(dry_run=True)

    assert result["status"] == "dry_run_ready"
    assert result["secret_printed"] is False
    assert {item["status"] for item in result["operations"]} == {"planned"}


def test_init_audit_infra_uses_direct_audit_user_not_sysdba():
    source = Path("init_audit_infra.py").read_text(encoding="utf-8")

    assert AUDIT_SCHEMA_OWNER == "AUDIT_USER"
    assert "AUTH_MODE_SYSDBA" not in source
    assert "SYSDBA" in source  # only documentation text, not connection mode
    assert "oracledb.connect(user=AUDIT_SCHEMA_OWNER" in source
