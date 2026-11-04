# Local Vault Readiness

This project is Vault-ready but does not require Vault to run.

## Persistence

- `vault server -dev` is temporary. When the process stops, data written to that dev server is lost.
- A local server started with `vault/vault-server.hcl.example` uses file-backed Raft storage under `vault/data`, so data can survive a PC restart.
- Even with persistent storage, Vault starts sealed after restart. You must unseal it before the app can read secrets.
- Root tokens and unseal keys must not be saved in this repository.

## App Configuration

The app reads secrets in this order:

1. Vault, when `VAULT_ENABLED=true` and Vault is reachable.
2. Environment variables loaded from `.env` / `.env.dev`.

Supported Vault settings:

```text
VAULT_ENABLED=true
VAULT_ADDR=http://127.0.0.1:8200
VAULT_TOKEN=<set-outside-repo>
VAULT_KV_MOUNT=secret
VAULT_APP_SECRET_PATH=dwh-studio/dev
VAULT_TIMEOUT_SECONDS=2
```

Store the same key names already used by `.env`, for example `ORACLE_USER`,
`ORACLE_PASSWORD`, `ORACLE_DSN`, `ANTHROPIC_API_KEY`, and ODI-related settings.

## Safe Local Plan

1. Keep using `.env.dev` while Vault is not installed.
2. Install/start Vault only after explicit approval.
3. Test with non-production secrets first.
4. Move real local secrets to Vault after backup, unseal, and token handling are clear.

## Key-name-only Inventory

Use `secret_inventory.py` to inspect readiness without printing secret values:

```powershell
.\.venv\Scripts\python.exe secret_inventory.py
```

The output includes only:

- key name
- source label: `vault`, `env`, or `missing`
- configured boolean
- rotation group

It intentionally omits values, value lengths, connection strings, token contents, and raw `.env` content.

## Deployment Decision

Current safe default for this workstation:

- Mode: local-only env fallback.
- Vault: ready in code, not required to run the app.
- Migration: blocked until an explicit approval exists for Vault start/install and credential rotation.

Vault-enabled mode is appropriate only after:

1. Vault storage, unseal, token handling, backup, and recovery are documented for the target machine.
2. Credentials are rotated or re-issued; do not copy historically exposed values as-is.
3. App smoke tests pass with Vault as the source and local env fallback still available for rollback.

## Rotation Checklist

1. Run the key-name-only inventory and confirm no values are printed.
2. Back up current operator secret files outside the project workspace.
3. Rotate app auth and Flask secret material.
4. Rotate Oracle app, DDL, source, target, and setup/admin credentials in an approved order.
5. Rotate ODI repository, ODI supervisor/runtime, and ODI REST/agent credentials.
6. Rotate WebLogic and optional Airflow credentials if those components are in scope.
7. Rotate LLM/API keys that may have existed in historical files.
8. Store rotated values in Vault only after Vault persistence and unseal/token handling are approved.
9. Restart the app and run smoke tests.
10. Re-run the inventory and confirm expected keys report `vault` source.
11. Keep local-env fallback until Vault-backed smoke is reviewed and accepted.
12. Remove or archive old local secret files only after rollback expectations are clear.
