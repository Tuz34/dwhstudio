import pytest

from deployment_security import build_deployment_profile, validate_deployment_security


def _settings(**overrides):
    settings = {
        "environment": "dev",
        "deployment_profile": "local-only",
        "app_host": "127.0.0.1",
        "app_debug": False,
        "auth_enabled": False,
        "auth_configured": False,
        "flask_secret_key_configured": False,
        "tls_enabled": False,
        "reverse_proxy_tls_enabled": False,
        "ip_allowlist": [],
        "allowed_origins": [],
    }
    settings.update(overrides)
    return settings


def test_local_only_profile_accepts_loopback_without_auth():
    profile = validate_deployment_security(_settings())

    assert profile["profile"] == "local-only"
    assert profile["network_exposed"] is False
    assert profile["status"] == "safe"


def test_local_only_profile_blocks_network_wide_host():
    with pytest.raises(RuntimeError, match="local-only"):
        validate_deployment_security(_settings(app_host="0.0.0.0"))


def test_network_profile_blocks_debug_mode():
    with pytest.raises(RuntimeError, match="debug"):
        validate_deployment_security(
            _settings(
                deployment_profile="lan-pilot",
                app_host="0.0.0.0",
                app_debug=True,
                auth_enabled=True,
                auth_configured=True,
                ip_allowlist=["10.0.0.0/24"],
            )
        )


def test_network_profile_requires_configured_auth():
    with pytest.raises(RuntimeError, match="auth"):
        validate_deployment_security(
            _settings(
                deployment_profile="lan-pilot",
                app_host="0.0.0.0",
                auth_enabled=True,
                auth_configured=False,
                ip_allowlist=["10.0.0.0/24"],
            )
        )


def test_lan_pilot_profile_reports_required_controls():
    settings = _settings(
        deployment_profile="lan-pilot",
        app_host="0.0.0.0",
        auth_enabled=True,
        auth_configured=True,
        tls_enabled=True,
        tls_cert_file="synthetic-cert.pem",
        tls_key_file="synthetic-key.pem",
        allowed_hosts=["studio.example.test"],
        ip_allowlist=["10.0.0.0/24"],
    )

    with pytest.raises(RuntimeError, match="configured Flask secret key"):
        validate_deployment_security(settings)

    profile = validate_deployment_security({**settings, "flask_secret_key_configured": True})

    assert profile["profile"] == "lan-pilot"
    assert profile["network_exposed"] is True
    assert profile["status"] == "review-required"
    assert "auth" in profile["active_controls"]
    assert "ip_allowlist" not in profile["active_controls"]
    assert profile["ip_allowlist_configured"] is True
    assert profile["ip_allowlist_enforced"] is False
    assert "session_key_configured" in profile["active_controls"]


def test_on_prem_hardened_requires_tls_boundary_and_secret_key():
    with pytest.raises(RuntimeError, match="TLS"):
        validate_deployment_security(
            _settings(
                environment="prod",
                deployment_profile="on-prem-hardened",
                app_host="0.0.0.0",
                auth_enabled=True,
                auth_configured=True,
                flask_secret_key_configured=True,
            )
        )


def test_build_profile_is_redacted_and_ui_safe():
    profile = build_deployment_profile(
        _settings(
            deployment_profile="on-prem-hardened",
            app_host="0.0.0.0",
            auth_enabled=True,
            auth_configured=True,
            flask_secret_key_configured=True,
            reverse_proxy_tls_enabled=True,
            ip_allowlist=["10.0.0.0/24"],
            allowed_origins=["https://dwh.example.local"],
        )
    )

    assert profile["profile"] == "on-prem-hardened"
    assert profile["network_exposed"] is True
    assert profile["status"] == "review-required"
    assert "secret" not in str(profile).lower()
    assert profile["allowed_origins_count"] == 1


def test_app_exposes_redacted_deployment_profile_endpoint():
    from app import app

    response = app.test_client().get("/api/deployment/security-profile")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["profile"] in {"local-only", "lan-pilot", "on-prem-hardened"}
    assert "secret" not in str(payload).lower()
