"""
Central secret lookup with optional HashiCorp Vault support.

The application keeps the current .env behavior unless Vault is explicitly
enabled with VAULT_ENABLED=true and VAULT_ADDR/VAULT_TOKEN are configured.
Secret values are never logged.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import requests

logger = logging.getLogger("dwh_app")


def _enabled() -> bool:
    return os.getenv("VAULT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _vault_url(path: str) -> str:
    addr = os.getenv("VAULT_ADDR", "").strip().rstrip("/")
    if not addr:
        raise RuntimeError("VAULT_ADDR is not configured")
    return f"{addr}/v1/{path.lstrip('/')}"


@lru_cache(maxsize=8)
def _read_vault_secret_bundle() -> dict[str, Any]:
    if not _enabled():
        return {}

    token = os.getenv("VAULT_TOKEN", "").strip()
    if not token:
        logger.warning("VAULT_ENABLED=true but VAULT_TOKEN is not configured; using env fallback.")
        return {}

    mount = os.getenv("VAULT_KV_MOUNT", "secret").strip().strip("/") or "secret"
    secret_path = os.getenv("VAULT_APP_SECRET_PATH", "dwh-studio/dev").strip().strip("/")
    timeout = float(os.getenv("VAULT_TIMEOUT_SECONDS", "2"))
    headers = {"X-Vault-Token": token}

    # Prefer KV v2, then fall back to KV v1 for small local installations.
    candidate_paths = (f"{mount}/data/{secret_path}", f"{mount}/{secret_path}")
    for candidate in candidate_paths:
        try:
            response = requests.get(_vault_url(candidate), headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning("Vault lookup failed; using env fallback. error=%s", exc.__class__.__name__)
            return {}

        if response.status_code == 404:
            continue
        if response.status_code >= 400:
            logger.warning("Vault lookup returned HTTP %s; using env fallback.", response.status_code)
            return {}

        payload = response.json().get("data") or {}
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
        return payload

    logger.warning("Vault secret path is not available; using env fallback.")
    return {}


def get_secret(name: str, default: str | None = None) -> str | None:
    """Return a named secret from Vault first, then environment variables."""
    value = _read_vault_secret_bundle().get(name)
    if value is not None:
        return str(value)
    return os.getenv(name, default)


def get_secret_list(name: str, default: str = "") -> list[str]:
    value = get_secret(name, default) or ""
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def vault_status() -> dict[str, Any]:
    bundle = _read_vault_secret_bundle()
    return {
        "enabled": _enabled(),
        "configured": bool(os.getenv("VAULT_ADDR") and os.getenv("VAULT_TOKEN")),
        "loaded_key_count": len(bundle),
        "mount": os.getenv("VAULT_KV_MOUNT", "secret"),
        "path": os.getenv("VAULT_APP_SECRET_PATH", "dwh-studio/dev"),
    }
