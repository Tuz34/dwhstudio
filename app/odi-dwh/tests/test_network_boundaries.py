"""Offline boundaries. Run full-app cases via tests.run_pr16_offline's guards."""

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask, request

import deployment_security as security


def settings(**overrides):
    result = dict(
        environment="test", deployment_profile="local-only", app_host="127.0.0.1",
        app_port=5000, app_debug=False, app_use_reloader=False, auth_enabled=False,
        auth_configured=False, flask_secret_key_configured=False, tls_enabled=False,
        reverse_proxy_tls_enabled=False, tls_cert_file="", tls_key_file="",
        allowed_hosts=[], allowed_origins=[], ip_allowlist=[],
    )
    result.update(overrides)
    return result


def network(**overrides):
    result = settings(
        deployment_profile="lan-pilot", auth_enabled=True, auth_configured=True,
        flask_secret_key_configured=True, reverse_proxy_tls_enabled=True,
        allowed_hosts=["studio.example.test", "studio.example.test:8443"],
        ip_allowlist=["192.0.2.0/24"],
    )
    result.update(overrides)
    return result


def isolated_app(options):
    app = Flask(__name__)
    events = []
    profile, run_options = security.configure_deployment(app, options)

    @app.before_request
    def fake_auth():
        events.append("auth")

    @app.route("/api/status", methods=["GET", "POST"])
    @app.route("/api/auth/login", methods=["GET", "POST"])
    @app.route("/api/private", methods=["GET", "POST"])
    @app.route("/")
    def public_or_private():
        events.append("route")
        return {"ok": True}

    return app, events, profile, run_options


@pytest.mark.parametrize("profile", ["lan-pilot", "on-prem-hardened"])
def test_both_network_profiles_reject_cleartext_even_on_loopback(profile):
    with pytest.raises(RuntimeError, match="TLS"):
        security.validate_deployment_security(network(
            deployment_profile=profile, reverse_proxy_tls_enabled=False,
        ))


@pytest.mark.parametrize("field,value", [
    ("auth_enabled", False), ("auth_configured", False), ("app_debug", True),
    ("flask_secret_key_configured", False), ("allowed_hosts", []),
])
def test_loopback_proxy_does_not_bypass_network_profile_controls(field, value):
    with pytest.raises(RuntimeError):
        security.validate_deployment_security(network(**{field: value}))


@pytest.mark.parametrize("overrides", [
    {"app_host": "0.0.0.0"}, {"app_host": "192.0.2.1"},
    {"tls_enabled": True},
])
def test_proxy_requires_loopback_and_unambiguous_mode(overrides):
    with pytest.raises(RuntimeError):
        security.validate_deployment_security(network(**overrides))


def test_direct_tls_flag_alone_is_not_a_transport():
    with pytest.raises(RuntimeError, match="certificate"):
        security.validate_deployment_security(network(
            tls_enabled=True, reverse_proxy_tls_enabled=False, app_host="0.0.0.0",
        ))


def test_direct_tls_loads_context_and_passes_it_to_server(monkeypatch):
    context = Mock()
    factory = Mock(return_value=context)
    monkeypatch.setattr(security.ssl, "SSLContext", factory)
    app, _, _, run_options = isolated_app(network(
        tls_enabled=True, reverse_proxy_tls_enabled=False, app_host="0.0.0.0",
        tls_cert_file="synthetic-cert.pem", tls_key_file="synthetic-key.pem",
    ))
    context.load_cert_chain.assert_called_once_with("synthetic-cert.pem", "synthetic-key.pem")
    assert run_options["ssl_context"] is context
    assert context.minimum_version == security.ssl.TLSVersion.TLSv1_2
    assert app.config["SESSION_COOKIE_SECURE"] is True
    client = app.test_client()
    assert client.get("/", base_url="https://studio.example.test").status_code == 200
    assert client.get("/", base_url="http://studio.example.test",
                      headers={"X-Forwarded-Proto": "https"}).status_code == 403


def test_bad_certificate_fails_before_install_or_run_without_path_leak(monkeypatch):
    context = Mock()
    context.load_cert_chain.side_effect = OSError("SYNTHETIC_PRIVATE_PATH")
    monkeypatch.setattr(security.ssl, "SSLContext", Mock(return_value=context))
    app = Flask(__name__)
    original = app.wsgi_app
    with pytest.raises(RuntimeError, match="TLS") as error:
        security.configure_deployment(app, network(
            tls_enabled=True, reverse_proxy_tls_enabled=False,
            tls_cert_file="synthetic-cert.pem", tls_key_file="synthetic-key.pem",
        ))
    assert "SYNTHETIC_PRIVATE_PATH" not in str(error.value)
    assert app.wsgi_app == original


@pytest.mark.parametrize("authority", [
    "localhost", "LOCALHOST:5000", "127.0.0.1:5000", "[::1]:5000", "[0:0:0:0:0:0:0:1]",
])
def test_local_http_legitimate_authorities(authority):
    app, events, _, options = isolated_app(settings())
    assert options["ssl_context"] is None
    assert app.config["SESSION_COOKIE_SECURE"] is False
    response = app.test_client().get("/api/status", environ_overrides={"HTTP_HOST": authority})
    assert response.status_code == 200
    assert events == ["auth", "route"]


@pytest.mark.parametrize("authority", [
    "evil.example", "localhost.evil.example", "evil-localhost", "localhost.",
    "127.1", "2130706433", "0177.0.0.1", "127.0.0.1.evil.example", "127.0.0.2",
    "localhost@evil.example", "evil.example@localhost", "http://localhost", "localhost/path",
    "localhost?x=1", "localhost#x", "localhost,evil.example", "localhost:0", "localhost:65536",
    "localhost:05000", "localhost:+5000", "localhost:", "localhost:5001", "localhost:5000:80",
    "::1", "[::1]evil", "[::1%25lo]:5000", "[::ffff:127.0.0.1]", "", " localhost",
    "localhost ", "local\thost", "localhost\r\nInjected: x", "loc\u0430lhost",
])
@pytest.mark.parametrize("path", ["/api/status", "/api/auth/login", "/api/private", "/"])
def test_invalid_host_rejected_before_any_auth_or_route(authority, path):
    app, events, _, _ = isolated_app(settings())
    response = app.test_client().get(path, environ_overrides={"HTTP_HOST": authority})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid request authority."}
    assert events == []


@pytest.mark.parametrize("peer,proto,expected", [
    ("127.0.0.1", "https", 200), ("::1", "https", 200),
    ("192.0.2.1", "https", 403), ("localhost", "https", 403),
    ("127.0.0.1", None, 403), ("127.0.0.1", "http", 403),
    ("127.0.0.1", "https,http", 403), ("127.0.0.1", " https", 403),
])
def test_proxy_requires_actual_loopback_peer_and_single_https_assertion(peer, proto, expected):
    app, events, _, options = isolated_app(network())
    assert options["host"] == "127.0.0.1" and options["ssl_context"] is None
    environ = {"REMOTE_ADDR": peer, "HTTP_HOST": "studio.example.test"}
    if proto is not None:
        environ["HTTP_X_FORWARDED_PROTO"] = proto
    response = app.test_client().post("/api/auth/login", environ_overrides=environ)
    assert response.status_code == expected
    assert bool(events) is (expected == 200)


def test_forwarded_host_and_for_cannot_override_authority_or_peer():
    app, events, _, _ = isolated_app(network())
    headers = {"X-Forwarded-Host": "studio.example.test", "X-Forwarded-For": "127.0.0.1",
               "X-Forwarded-Proto": "https", "Forwarded": "for=127.0.0.1;host=studio.example.test;proto=https"}
    client = app.test_client()
    assert client.get("/", headers=headers, environ_overrides={
        "HTTP_HOST": "evil.example", "REMOTE_ADDR": "127.0.0.1",
    }).status_code == 400
    assert client.get("/", headers=headers, environ_overrides={
        "HTTP_HOST": "studio.example.test", "REMOTE_ADDR": "192.0.2.1",
    }).status_code == 403
    assert events == []


def test_verified_proxy_scheme_reaches_flask_without_rewriting_host_or_peer():
    app, _, _, _ = isolated_app(network())

    @app.get("/transport")
    def transport():
        return {"secure": request.is_secure, "host": request.host, "peer": request.remote_addr}

    response = app.test_client().get("/transport", environ_overrides={
        "HTTP_HOST": "studio.example.test", "REMOTE_ADDR": "127.0.0.1",
        "HTTP_X_FORWARDED_PROTO": "https", "HTTP_X_FORWARDED_HOST": "evil.example",
        "HTTP_X_FORWARDED_FOR": "192.0.2.10", "wsgi.url_scheme": "http",
    })
    assert response.get_json() == {
        "secure": True, "host": "studio.example.test", "peer": "127.0.0.1",
    }


def test_proxy_preserves_csrf_token_and_secure_referrer_checks():
    from flask_wtf.csrf import CSRFProtect, generate_csrf

    app, _, _, _ = isolated_app(network())
    app.secret_key = bytes(range(32))
    CSRFProtect(app)

    @app.get("/token")
    def token():
        return {"csrf": generate_csrf()}

    client = app.test_client()
    options = dict(base_url="https://studio.example.test", environ_overrides={
        "REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_PROTO": "https", "wsgi.url_scheme": "http",
    })
    csrf = client.get("/token", **options).get_json()["csrf"]
    good = {"X-CSRFToken": csrf, "Referer": "https://studio.example.test/"}
    assert client.post("/api/private", headers=good, **options).status_code == 200
    assert client.post("/api/private", **options).status_code == 400
    assert client.post("/api/private", headers={
        **good, "Referer": "https://evil.example/",
    }, **options).status_code == 400


@pytest.mark.parametrize("peer", ["192.0.2.1", "localhost", None, "::ffff:127.0.0.1"])
def test_local_profile_cannot_be_exposed_by_an_alternate_wsgi_bind(peer):
    app, events, _, _ = isolated_app(settings())
    response = app.test_client().get("/api/status", environ_overrides={
        "HTTP_HOST": "localhost:5000", "REMOTE_ADDR": peer,
        "HTTP_X_FORWARDED_FOR": "127.0.0.1",
    })
    assert response.status_code == 403
    assert events == []


@pytest.mark.parametrize("authority,expected", [
    ("studio.example.test", 200), ("STUDIO.EXAMPLE.TEST:443", 200),
    ("studio.example.test:8443", 200), ("studio.example.test:5000", 400),
    ("studio.example.test.evil.example", 400), ("other.example.test", 400),
])
def test_network_authorities_match_exactly(authority, expected):
    app, _, _, _ = isolated_app(network())
    assert app.test_client().get("/", environ_overrides={
        "HTTP_HOST": authority, "REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_PROTO": "https",
    }).status_code == expected


@pytest.mark.parametrize("hosts", [["*.example.test"], [".example.test"], ["https://example.test"],
                                 ["example.test/path"], ["example.test:65536"], ["::1"]])
def test_invalid_configured_hosts_fail_startup(hosts):
    with pytest.raises(RuntimeError, match="APP_ALLOWED_HOSTS"):
        security.validate_deployment_security(network(allowed_hosts=hosts))


def test_report_only_controls_are_not_claimed_enforced():
    profile = security.build_deployment_profile(network(allowed_origins=["https://studio.example.test"]))
    assert "ip_allowlist" not in profile["active_controls"]
    assert "allowed_origins" not in profile["active_controls"]
    assert profile["ip_allowlist_enforced"] is False
    assert profile["allowed_origins_enforced"] is False


def test_app_startup_wires_boundary_and_uses_exact_run_options():
    # Inspect only wiring: never execute app imports or real configuration.
    tree = ast.parse((Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8-sig"))
    call = next(n for n in tree.body if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                and n.value.func.id == "configure_deployment")
    assert ast.unparse(call) == (
        "_DEPLOYMENT_SECURITY_PROFILE, _DEPLOYMENT_RUN_OPTIONS = "
        "configure_deployment(app, _DEPLOYMENT_SETTINGS)"
    )
    config_assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == "_DEPLOYMENT_SETTINGS"
                                     for t in n.targets))
    pairs = {k.value: ast.unparse(v) for k, v in zip(config_assignment.value.keys,
                                                   config_assignment.value.values)}
    for key in ("app_host", "app_port", "app_debug", "app_use_reloader", "tls_enabled",
                "reverse_proxy_tls_enabled", "tls_cert_file", "tls_key_file", "allowed_hosts"):
        assert pairs[key] == (key.upper() if key.startswith("app_") else "APP_" + key.upper())
    main = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    assert len(main.body) == 1
    assert ast.unparse(main.body[0]) == "app.run(**_DEPLOYMENT_RUN_OPTIONS)"


@pytest.mark.parametrize("authority", ["192.0.2.10:5443", "[2001:db8::1]:5443"])
def test_explicit_network_ip_authorities_are_supported(authority):
    app, _, _, _ = isolated_app(network(allowed_hosts=[authority]))
    assert app.test_client().get("/", environ_overrides={
        "HTTP_HOST": authority, "REMOTE_ADDR": "::1", "HTTP_X_FORWARDED_PROTO": "https",
    }).status_code == 200


def test_host_checks_do_not_resolve_dns(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("DNS resolution is not an authority check")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "gethostbyname", forbidden)
    app, _, _, _ = isolated_app(network())
    assert app.test_client().get("/", environ_overrides={
        "HTTP_HOST": "studio.example.test", "REMOTE_ADDR": "::1", "HTTP_X_FORWARDED_PROTO": "https",
    }).status_code == 200


@pytest.mark.parametrize("profile", ["lan-pilot", "on-prem-hardened"])
def test_network_profiles_have_legitimate_proxy_configuration(profile):
    app, _, report, options = isolated_app(network(deployment_profile=profile))
    assert options["ssl_context"] is None
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert report["tls_boundary"] == "reverse_proxy"
    assert "transport_boundary" in report["active_controls"]


def test_local_profile_cannot_authorize_rebinding_hostname():
    with pytest.raises(RuntimeError, match="local-only"):
        security.validate_deployment_security(settings(allowed_hosts=["local.example.test"]))


@pytest.mark.parametrize("path,method", [
    ("/api/status", "GET"), ("/api/auth/login", "POST"), ("/api/auth/logout", "POST"),
    ("/api/deployment/security-profile", "GET"), ("/api/private", "POST"),
    ("/", "GET"), ("/static/missing.css", "GET"),
])
def test_real_app_rejects_host_before_existing_auth_and_audit(monkeypatch, path, method):
    # The guarded runner injects config and prohibits live IO before this import.
    import app as application

    def forbidden(*args, **kwargs):
        raise AssertionError("Untrusted authority reached application controls")

    monkeypatch.setattr(application, "_auth_enabled", forbidden)
    monkeypatch.setattr(application, "_record_security_event", forbidden)
    response = application.app.test_client().open(path, method=method, environ_overrides={
        "HTTP_HOST": "localhost.attacker.example", "REMOTE_ADDR": "127.0.0.1",
    })
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid request authority."}
