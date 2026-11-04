import os
from pathlib import Path

import secret_inventory
import secret_provider


PROJECT_ROOT = Path(__file__).parents[1]


def test_config_startup_log_does_not_include_oracle_identity_or_dsn():
    source = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")

    assert "_dsn_display" not in source
    assert "Oracle kullanıcı" not in source
    assert '"Runtime configuration loaded: environment=%s oracle_configured=%s"' in source


def test_secret_inventory_reports_key_names_without_values(monkeypatch):
    secret_provider._read_vault_secret_bundle.cache_clear()
    monkeypatch.delenv("VAULT_ENABLED", raising=False)
    monkeypatch.delenv("ODI_REPO_PASSWORD", raising=False)
    monkeypatch.setenv("ORACLE_PASSWORD", "super-secret-value")

    report = secret_inventory.build_secret_inventory(["ORACLE_PASSWORD", "ODI_REPO_PASSWORD"])

    assert report["mode"] == "local-env"
    assert report["items"][0]["key"] == "ORACLE_PASSWORD"
    assert report["items"][0]["source"] == "env"
    assert report["items"][0]["configured"] is True
    assert report["items"][1]["key"] == "ODI_REPO_PASSWORD"
    assert report["items"][1]["source"] == "missing"
    assert "super-secret-value" not in repr(report)


def test_secret_inventory_reports_vault_precedence_without_values(monkeypatch):
    secret_provider._read_vault_secret_bundle.cache_clear()
    monkeypatch.setenv("VAULT_ENABLED", "true")
    monkeypatch.setenv("VAULT_ADDR", "http://127.0.0.1:8200")
    monkeypatch.setenv("VAULT_TOKEN", "token-for-test")
    monkeypatch.setenv("ORACLE_PASSWORD", "env-password")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"data": {"ORACLE_PASSWORD": "vault-password"}}}

    monkeypatch.setattr(secret_provider.requests, "get", lambda *args, **kwargs: Response())

    report = secret_inventory.build_secret_inventory(["ORACLE_PASSWORD"])

    assert report["mode"] == "vault-enabled"
    assert report["items"] == [
        {
            "key": "ORACLE_PASSWORD",
            "source": "vault",
            "configured": True,
            "rotation_group": "oracle",
        }
    ]
    assert "vault-password" not in repr(report)
    assert "env-password" not in repr(report)


def test_secret_inventory_json_output_is_redacted(monkeypatch):
    secret_provider._read_vault_secret_bundle.cache_clear()
    monkeypatch.delenv("VAULT_ENABLED", raising=False)
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "local-api-key")

    text = secret_inventory.inventory_json(["LOCAL_OPENAI_API_KEY"])

    assert "LOCAL_OPENAI_API_KEY" in text
    assert "local-api-key" not in text
