"""Key-name-only secret inventory for migration and rotation planning.

This module deliberately reports only key names, source labels, and rotation
groups. It must never return or print secret values.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable

import secret_provider


@dataclass(frozen=True)
class SecretKeySpec:
    key: str
    rotation_group: str


SECRET_KEY_SPECS: tuple[SecretKeySpec, ...] = (
    SecretKeySpec("APP_AUTH_PASSWORD_HASH", "app-auth"),
    SecretKeySpec("FLASK_SECRET_KEY", "app-auth"),
    SecretKeySpec("LOCAL_OPENAI_API_KEY", "llm"),
    SecretKeySpec("ANTHROPIC_API_KEY", "llm"),
    SecretKeySpec("ORACLE_USER", "oracle"),
    SecretKeySpec("ORACLE_PASSWORD", "oracle"),
    SecretKeySpec("ORACLE_DSN", "oracle"),
    SecretKeySpec("ORACLE_DDL_USER", "oracle-ddl"),
    SecretKeySpec("ORACLE_DDL_PASSWORD", "oracle-ddl"),
    SecretKeySpec("SOURCE_ORACLE_USER", "source-oracle"),
    SecretKeySpec("SOURCE_ORACLE_PASSWORD", "source-oracle"),
    SecretKeySpec("SOURCE_ORACLE_DSN", "source-oracle"),
    SecretKeySpec("TARGET_ORACLE_USER", "target-oracle"),
    SecretKeySpec("TARGET_ORACLE_PASSWORD", "target-oracle"),
    SecretKeySpec("TARGET_ORACLE_DSN", "target-oracle"),
    SecretKeySpec("MSSQL_SERVER", "mssql-optional"),
    SecretKeySpec("MSSQL_DATABASE", "mssql-optional"),
    SecretKeySpec("MSSQL_USER", "mssql-optional"),
    SecretKeySpec("MSSQL_PASSWORD", "mssql-optional"),
    SecretKeySpec("ODI_REPO_DSN", "odi-repository"),
    SecretKeySpec("ODI_REPO_USER", "odi-repository"),
    SecretKeySpec("ODI_REPO_PASSWORD", "odi-repository"),
    SecretKeySpec("ODI_SUPERVISOR_USER", "odi-runtime"),
    SecretKeySpec("ODI_SUPERVISOR_PASSWORD", "odi-runtime"),
    SecretKeySpec("ODI_REST_URL", "odi-agent"),
    SecretKeySpec("ODI_REST_USER", "odi-agent"),
    SecretKeySpec("ODI_REST_PASSWORD", "odi-agent"),
    SecretKeySpec("WEBLOGIC_PASSWORD", "weblogic"),
    SecretKeySpec("AIRFLOW_DB_PASSWORD", "airflow-optional"),
    SecretKeySpec("_AIRFLOW_WWW_USER_PASSWORD", "airflow-optional"),
    SecretKeySpec("SETUP_APP_USER_PASSWORD", "rotation-setup"),
    SecretKeySpec("APP_USER_PASSWORD", "rotation-setup"),
    SecretKeySpec("SETUP_AUDIT_USER_PASSWORD", "rotation-setup"),
    SecretKeySpec("AUDIT_USER_PASSWORD", "rotation-setup"),
    SecretKeySpec("SETUP_ODI_EXEC_PASSWORD", "rotation-setup"),
    SecretKeySpec("ODI_EXEC_PASSWORD", "rotation-setup"),
    SecretKeySpec("SETUP_ODS_OWNER_PASSWORD", "rotation-setup"),
    SecretKeySpec("ODS_OWNER_PASSWORD", "rotation-setup"),
    SecretKeySpec("SETUP_DWH_OWNER_PASSWORD", "rotation-setup"),
    SecretKeySpec("DWH_OWNER_PASSWORD", "rotation-setup"),
    SecretKeySpec("ORACLE_SYS_PASSWORD", "admin-setup"),
    SecretKeySpec("SETUP_ORACLE_SYS_PASSWORD", "admin-setup"),
    SecretKeySpec("VAULT_TOKEN", "vault-bootstrap"),
)


def _spec_by_key() -> dict[str, SecretKeySpec]:
    return {spec.key: spec for spec in SECRET_KEY_SPECS}


def _vault_bundle_keys() -> set[str]:
    return {str(key) for key in secret_provider._read_vault_secret_bundle().keys()}


def build_secret_inventory(keys: Iterable[str] | None = None) -> dict[str, object]:
    """Return key-name-only secret source metadata.

    The returned object intentionally omits secret values and value lengths.
    """

    specs = _spec_by_key()
    selected_keys = list(keys) if keys is not None else [spec.key for spec in SECRET_KEY_SPECS]
    vault_keys = _vault_bundle_keys()
    items: list[dict[str, object]] = []

    for key in selected_keys:
        if key in vault_keys:
            source = "vault"
            configured = True
        elif os.getenv(key) is not None:
            source = "env"
            configured = True
        else:
            source = "missing"
            configured = False

        spec = specs.get(key, SecretKeySpec(key, "unclassified"))
        items.append(
            {
                "key": key,
                "source": source,
                "configured": configured,
                "rotation_group": spec.rotation_group,
            }
        )

    return {
        "mode": "vault-enabled" if secret_provider.vault_status()["enabled"] else "local-env",
        "vault": {
            "enabled": secret_provider.vault_status()["enabled"],
            "configured": secret_provider.vault_status()["configured"],
            "loaded_key_count": secret_provider.vault_status()["loaded_key_count"],
        },
        "items": items,
        "value_policy": "key-name-only; values and lengths are intentionally omitted",
    }


def inventory_json(keys: Iterable[str] | None = None) -> str:
    return json.dumps(build_secret_inventory(keys), indent=2, sort_keys=True)


def rotation_checklist() -> list[dict[str, str]]:
    return [
        {
            "phase": "1-inventory",
            "action": "Run key-name-only inventory and confirm no values are printed.",
        },
        {
            "phase": "2-backup",
            "action": "Back up current operator secret files outside the project workspace.",
        },
        {
            "phase": "3-rotate",
            "action": "Rotate Oracle, ODI, WebLogic, app auth, and LLM credentials in approved order.",
        },
        {
            "phase": "4-vault",
            "action": "Store rotated values in Vault only after Vault start/unseal/token handling is approved.",
        },
        {
            "phase": "5-verify",
            "action": "Restart app, run smoke tests, and confirm inventory reports vault source where expected.",
        },
        {
            "phase": "6-rollback",
            "action": "Keep local-env fallback available until Vault-backed smoke is proven and reviewed.",
        },
    ]


if __name__ == "__main__":
    print(inventory_json())
