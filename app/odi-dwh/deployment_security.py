"""Deployment security profile validation.

This module is intentionally local and side-effect free. It does not change
firewall, certificate, service, DB, or ODI state; it only validates runtime
configuration before the Flask app is exposed.
"""

from __future__ import annotations

from typing import Any


SUPPORTED_PROFILES = {"local-only", "lan-pilot", "on-prem-hardened"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
NETWORK_WIDE_HOSTS = {"0.0.0.0", "::", ""}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _profile(settings: dict[str, Any]) -> str:
    profile = str(settings.get("deployment_profile") or "local-only").strip().lower()
    if profile not in SUPPORTED_PROFILES:
        raise RuntimeError(f"Unsupported deployment profile: {profile}")
    return profile


def _host(settings: dict[str, Any]) -> str:
    return str(settings.get("app_host") or "127.0.0.1").strip().lower()


def is_network_exposed_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized in LOOPBACK_HOSTS:
        return False
    if normalized in NETWORK_WIDE_HOSTS:
        return True
    return True


def build_deployment_profile(settings: dict[str, Any]) -> dict[str, Any]:
    profile = _profile(settings)
    host = _host(settings)
    network_exposed = is_network_exposed_host(host)
    debug = _truthy(settings.get("app_debug"))
    auth_enabled = _truthy(settings.get("auth_enabled"))
    auth_configured = _truthy(settings.get("auth_configured"))
    tls_enabled = _truthy(settings.get("tls_enabled"))
    reverse_proxy_tls_enabled = _truthy(settings.get("reverse_proxy_tls_enabled"))
    secret_key_configured = _truthy(settings.get("flask_secret_key_configured"))
    ip_allowlist = _as_list(settings.get("ip_allowlist"))
    allowed_origins = _as_list(settings.get("allowed_origins"))

    active_controls: list[str] = []
    if not network_exposed:
        active_controls.append("loopback")
    if auth_enabled and auth_configured:
        active_controls.append("auth")
    if tls_enabled:
        active_controls.append("tls")
    if reverse_proxy_tls_enabled:
        active_controls.append("reverse_proxy_tls")
    if ip_allowlist:
        active_controls.append("ip_allowlist")
    if allowed_origins:
        active_controls.append("allowed_origins")
    if secret_key_configured:
        active_controls.append("session_key_configured")

    status = "safe"
    if profile == "lan-pilot" or (network_exposed and profile != "on-prem-hardened"):
        status = "review-required"
    if profile == "on-prem-hardened" and network_exposed and not (tls_enabled or reverse_proxy_tls_enabled):
        status = "blocked"

    return {
        "profile": profile,
        "environment": str(settings.get("environment") or "dev").strip().lower(),
        "host": host,
        "network_exposed": network_exposed,
        "debug": debug,
        "auth_enabled": auth_enabled,
        "auth_configured": auth_configured,
        "tls_boundary": "direct" if tls_enabled else ("reverse_proxy" if reverse_proxy_tls_enabled else "none"),
        "ip_allowlist_configured": bool(ip_allowlist),
        "allowed_origins_count": len(allowed_origins),
        "active_controls": active_controls,
        "status": status,
        "supported_profiles": sorted(SUPPORTED_PROFILES),
    }


def validate_deployment_security(settings: dict[str, Any]) -> dict[str, Any]:
    result = build_deployment_profile(settings)
    errors: list[str] = []

    if result["profile"] == "local-only" and result["network_exposed"]:
        errors.append("local-only profile requires APP_HOST to stay on loopback.")
    if result["network_exposed"] and result["debug"]:
        errors.append("Network-exposed deployment cannot run with debug mode enabled.")
    if result["network_exposed"] and not result["auth_enabled"]:
        errors.append("Network-exposed deployment requires auth to be enabled.")
    if result["network_exposed"] and not result["auth_configured"]:
        errors.append("Network-exposed deployment requires configured auth credentials.")
    if "session_key_configured" not in result["active_controls"]:
        if result["environment"] == "prod":
            errors.append("prod deployment requires a configured Flask secret key.")
        elif result["network_exposed"] or result["profile"] != "local-only":
            errors.append("Network-exposed deployment requires a configured Flask secret key.")
    if result["profile"] == "on-prem-hardened" and result["tls_boundary"] == "none":
        errors.append("on-prem-hardened deployment requires a TLS boundary.")
    if result["profile"] == "on-prem-hardened" and not result["ip_allowlist_configured"]:
        errors.append("on-prem-hardened deployment requires an IP allowlist or equivalent network ACL.")

    if errors:
        raise RuntimeError("Deployment security validation failed: " + " ".join(errors))
    return result
