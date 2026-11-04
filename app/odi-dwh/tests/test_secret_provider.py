import secret_provider


def test_get_secret_uses_env_when_vault_disabled(monkeypatch):
    secret_provider._read_vault_secret_bundle.cache_clear()
    monkeypatch.delenv("VAULT_ENABLED", raising=False)
    monkeypatch.setenv("ORACLE_USER", "app_user")

    assert secret_provider.get_secret("ORACLE_USER") == "app_user"


def test_get_secret_prefers_vault_kv_v2(monkeypatch):
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

    def fake_get(url, headers, timeout):
        assert url == "http://127.0.0.1:8200/v1/secret/data/dwh-studio/dev"
        assert headers == {"X-Vault-Token": "token-for-test"}
        assert timeout == 2.0
        return Response()

    monkeypatch.setattr(secret_provider.requests, "get", fake_get)

    assert secret_provider.get_secret("ORACLE_PASSWORD") == "vault-password"
