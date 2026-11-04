import logging

import pytest
from flask import Flask

from app import _configure_flask_secret_key
from deployment_security import validate_deployment_security


@pytest.mark.parametrize(
    "weak_value",
    ["", "   ", "REPLACE_ME", "change_me", "CHANGEME", "secret", "password", "dev", "test", "default", "short-key"],
)
def test_weak_or_placeholder_key_generates_runtime_session_key(weak_value, caplog):
    flask_app = Flask(__name__)

    with caplog.at_level(logging.WARNING, logger="dwh_app"):
        configured = _configure_flask_secret_key(flask_app, weak_value)

    assert configured is False
    assert flask_app.secret_key != weak_value
    assert len(flask_app.secret_key) == 64
    assert "FLASK_SECRET_KEY" in caplog.text
    assert "REPLACE_ME" not in caplog.text


def test_strong_session_key_is_used_as_configured(caplog):
    flask_app = Flask(__name__)
    strong_key = "sample-strong-session-key-for-tests"

    with caplog.at_level(logging.WARNING, logger="dwh_app"):
        configured = _configure_flask_secret_key(flask_app, strong_key)

    assert configured is True
    assert flask_app.secret_key == strong_key
    assert not caplog.records


@pytest.mark.parametrize("length, configured", [(15, False), (16, True)])
def test_session_key_length_boundary(length, configured):
    flask_app = Flask(__name__)
    candidate = "x" * length

    assert _configure_flask_secret_key(flask_app, candidate) is configured
    assert (flask_app.secret_key == candidate) is configured


def test_whitespace_padded_placeholder_uses_runtime_key():
    flask_app = Flask(__name__)
    candidate = "  REPLACE_ME  "

    assert _configure_flask_secret_key(flask_app, candidate) is False
    assert flask_app.secret_key != candidate


@pytest.mark.parametrize(
    "candidate",
    ["replace_me_with_real_key", "change_me_now_with_suffix", "DEFAULT_RANDOM_SESSION_KEY"],
)
def test_placeholder_prefix_uses_runtime_key(candidate):
    flask_app = Flask(__name__)

    assert _configure_flask_secret_key(flask_app, candidate) is False
    assert flask_app.secret_key != candidate


def test_network_profile_rejects_placeholder_session_key():
    flask_app = Flask(__name__)
    configured = _configure_flask_secret_key(flask_app, "REPLACE_ME")

    with pytest.raises(RuntimeError, match="configured Flask secret key"):
        validate_deployment_security({
            "environment": "dev",
            "deployment_profile": "lan-pilot",
            "app_host": "0.0.0.0",
            "auth_enabled": True,
            "auth_configured": True,
            "flask_secret_key_configured": configured,
        })


def test_on_prem_hardened_profile_requires_configured_session_key():
    with pytest.raises(RuntimeError, match="configured Flask secret key"):
        validate_deployment_security({
            "environment": "dev",
            "deployment_profile": "on-prem-hardened",
            "app_host": "0.0.0.0",
            "auth_enabled": True,
            "auth_configured": True,
            "flask_secret_key_configured": False,
            "tls_enabled": True,
            "ip_allowlist": ["10.0.0.0/24"],
        })


def test_local_profile_accepts_generated_session_key():
    flask_app = Flask(__name__)
    configured = _configure_flask_secret_key(flask_app, "REPLACE_ME")

    profile = validate_deployment_security({
        "environment": "dev",
        "deployment_profile": "local-only",
        "app_host": "127.0.0.1",
        "flask_secret_key_configured": configured,
    })

    assert configured is False
    assert profile["network_exposed"] is False
    assert "session_key_configured" not in profile["active_controls"]
