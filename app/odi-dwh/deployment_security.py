"""Deployment security profile validation.

Validation is local and side-effect free. Startup wiring loads a configured TLS
certificate and installs a WSGI boundary; it never starts a listener or changes
firewall, service, DB, or ODI state.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
from typing import Any

from werkzeug.wrappers import Response

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


def _authority(value: Any, default_port: int) -> tuple[str, int]:
    """Parse an ASCII authority without DNS, URL reinterpretation or suffix matching."""
    if not isinstance(value, str) or not value or len(value) > 259:
        raise ValueError("Invalid authority")
    if not value.isascii() or re.search(r"[\s/@\\?#,%]", value):
        raise ValueError("Invalid authority")
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9A-Fa-f:]+)\](?::([0-9]+))?", value)
        if match is None:
            raise ValueError("Invalid authority")
        address = ipaddress.IPv6Address(match[1])
        host, port_text = str(address), match[2]
    else:
        match = re.fullmatch(r"([^:]+)(?::([0-9]+))?", value)
        if match is None:
            raise ValueError("Invalid authority")
        host, port_text = match[1].lower(), match[2]
        if re.fullmatch(r"[0-9.]+", host):
            host = str(ipaddress.IPv4Address(host))
        elif len(host) > 253 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        ):
            raise ValueError("Invalid authority")
    if port_text is None:
        return host, default_port
    if len(port_text) > 5 or port_text.startswith("0") or not 1 <= int(port_text) <= 65535:
        raise ValueError("Invalid authority")
    return host, int(port_text)


def _app_port(settings: dict[str, Any]) -> int:
    port = settings.get("app_port", 5000)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("APP_PORT must be an integer between 1 and 65535.")
    return port


def _allowed_authorities(settings: dict[str, Any]) -> frozenset[tuple[str, int]]:
    secure = _truthy(settings.get("tls_enabled")) or _truthy(settings.get("reverse_proxy_tls_enabled"))
    default_port = 443 if secure else 80
    port = _app_port(settings)
    authorities = {(host, p) for host in LOOPBACK_HOSTS for p in (default_port, port)}
    configured = _as_list(settings.get("allowed_hosts"))
    if _profile(settings) != "local-only" and not configured:
        raise RuntimeError("Network profiles require explicit APP_ALLOWED_HOSTS authorities.")
    for value in configured:
        try:
            authority = _authority(value, default_port)
        except ValueError:
            raise RuntimeError("APP_ALLOWED_HOSTS contains an invalid authority.") from None
        if _profile(settings) == "local-only" and authority[0] not in LOOPBACK_HOSTS:
            raise RuntimeError("local-only APP_ALLOWED_HOSTS must remain loopback.")
        authorities.add(authority)
    return frozenset(authorities)


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
    if secret_key_configured:
        active_controls.append("session_key_configured")

    status = "safe"
    if profile != "local-only" or network_exposed:
        status = "review-required"
    if profile != "local-only" and not (tls_enabled or reverse_proxy_tls_enabled):
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
        "ip_allowlist_enforced": False,
        "allowed_origins_count": len(allowed_origins),
        "allowed_origins_enforced": False,
        "active_controls": active_controls,
        "status": status,
        "supported_profiles": sorted(SUPPORTED_PROFILES),
    }


def validate_deployment_security(settings: dict[str, Any]) -> dict[str, Any]:
    result = build_deployment_profile(settings)
    errors: list[str] = []
    network_profile = result["profile"] != "local-only"
    protected = network_profile or result["network_exposed"]

    if result["profile"] == "local-only" and result["network_exposed"]:
        errors.append("local-only profile requires APP_HOST to stay on loopback.")
    if protected and result["debug"]:
        errors.append("Network-exposed deployment cannot run with debug mode enabled.")
    if protected and not result["auth_enabled"]:
        errors.append("Network-exposed deployment requires auth to be enabled.")
    if protected and not result["auth_configured"]:
        errors.append("Network-exposed deployment requires configured auth credentials.")
    if "session_key_configured" not in result["active_controls"]:
        if result["environment"] == "prod":
            errors.append("prod deployment requires a configured Flask secret key.")
        elif result["network_exposed"] or result["profile"] != "local-only":
            errors.append("Network-exposed deployment requires a configured Flask secret key.")
    if network_profile and result["tls_boundary"] == "none":
        errors.append("Network profiles require a TLS boundary.")
    direct = _truthy(settings.get("tls_enabled"))
    proxy = _truthy(settings.get("reverse_proxy_tls_enabled"))
    if direct and proxy:
        errors.append("Choose either direct TLS or reverse proxy TLS, not both.")
    if direct and not (settings.get("tls_cert_file") and settings.get("tls_key_file")):
        errors.append("Direct TLS requires a certificate and private key file.")
    if proxy and (result["network_exposed"] or not network_profile):
        errors.append("Reverse proxy TLS requires a network profile and a loopback APP_HOST.")
    if result["profile"] == "on-prem-hardened" and not result["ip_allowlist_configured"]:
        errors.append("on-prem-hardened deployment requires an IP allowlist or equivalent network ACL.")

    if errors:
        raise RuntimeError("Deployment security validation failed: " + " ".join(errors))
    _allowed_authorities(settings)
    return result


class DeploymentBoundary:
    """Reject untrusted authority/transport before Flask routing, auth or extensions."""

    def __init__(self, application: Any, settings: dict[str, Any], transport: str) -> None:
        self.application = application
        self.authorities = _allowed_authorities(settings)
        self.transport = transport
        self.local_only = _profile(settings) == "local-only"
        self.default_port = 80 if transport == "none" else 443

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        try:
            accepted = _authority(environ.get("HTTP_HOST"), self.default_port) in self.authorities
        except ValueError:
            accepted = False
        if not accepted:
            return Response('{"error":"Invalid request authority."}', status=400,
                            mimetype="application/json")(environ, start_response)
        if self.local_only and environ.get("REMOTE_ADDR") not in {"127.0.0.1", "::1"}:
            return Response('{"error":"Loopback access required."}', status=403,
                            mimetype="application/json")(environ, start_response)
        secure = True
        if self.transport == "direct":
            secure = environ.get("wsgi.url_scheme") == "https"
        elif self.transport == "reverse_proxy":
            # Only the raw socket peer is trusted. No ProxyFix / forwarded-host rewriting.
            secure = (environ.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}
                      and environ.get("HTTP_X_FORWARDED_PROTO") == "https")
        if not secure:
            return Response('{"error":"HTTPS transport required."}', status=403,
                            mimetype="application/json")(environ, start_response)
        if self.transport == "reverse_proxy":
            # Preserve HTTPS-aware CSRF/URL behavior only after the trusted-peer check.
            environ["wsgi.url_scheme"] = "https"
        return self.application(environ, start_response)


def configure_deployment(flask_app: Any, settings: dict[str, Any]) -> tuple[dict, dict]:
    """Fail before exposure; return the only options used by the built-in server."""
    profile = validate_deployment_security(settings)
    context = None
    if profile["tls_boundary"] == "direct":
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(settings["tls_cert_file"], settings["tls_key_file"])
        except (OSError, ValueError, TypeError):
            raise RuntimeError("Direct TLS certificate/key could not be loaded.") from None
    options = dict(host=profile["host"], port=_app_port(settings), debug=profile["debug"],
                   use_reloader=_truthy(settings.get("app_use_reloader")), ssl_context=context)
    flask_app.wsgi_app = DeploymentBoundary(flask_app.wsgi_app, settings, profile["tls_boundary"])
    flask_app.config["SESSION_COOKIE_SECURE"] = profile["tls_boundary"] != "none"
    profile["active_controls"].append("host_authority")
    if profile["tls_boundary"] != "none":
        profile["active_controls"].append("transport_boundary")
    return profile, options
