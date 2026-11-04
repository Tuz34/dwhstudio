# Deployment Security Profiles

This runbook defines how the local DWH Studio app may be exposed. It is a
configuration and review guide only. It does not install a service, change
firewall rules, create certificates, or start a reverse proxy.

## Profiles

### local-only

Default profile for development and single-user work.

- `DEPLOYMENT_PROFILE=local-only`
- `APP_HOST=127.0.0.1`
- `APP_DEBUG=false`
- Auth may remain disabled for local single-user work.
- No LAN access is intended.

The app fails fast if this profile is combined with a network-wide host such as
`0.0.0.0`.

### lan-pilot

Controlled pilot profile for a trusted internal network segment.

- `DEPLOYMENT_PROFILE=lan-pilot`
- `APP_HOST=0.0.0.0` only with explicit approval.
- `APP_DEBUG=false`
- `APP_AUTH_ENABLED=true`
- `APP_AUTH_USERNAME` and `APP_AUTH_PASSWORD_HASH` must be configured.
- `APP_IP_ALLOWLIST` should be set to the approved network segment or host list.
- Prefer a reverse proxy/TLS boundary if more than one operator will use it.

This profile is review-required. It is not a hardened institutional deployment.

### on-prem-hardened

Target profile for institutional/on-prem use.

- `DEPLOYMENT_PROFILE=on-prem-hardened`
- `APP_DEBUG=false`
- `APP_AUTH_ENABLED=true`
- `APP_AUTH_USERNAME` and `APP_AUTH_PASSWORD_HASH` must be configured.
- `FLASK_SECRET_KEY` must be configured with a non-placeholder value.
- TLS must be provided directly or through an approved reverse proxy:
  - `APP_TLS_ENABLED=true`, or
  - `APP_REVERSE_PROXY_TLS_ENABLED=true`
- `APP_IP_ALLOWLIST` must be set or an equivalent firewall/network ACL must be
  documented by the operator.
- Allowed browser origins should be listed in `APP_ALLOWED_ORIGINS` when the app
  is accessed through a named host.

## Blocked combinations

The Flask startup validator blocks these combinations:

- `local-only` with a network-wide host.
- Any network-exposed host with `APP_DEBUG=true`.
- Any network-exposed host without auth enabled.
- Any network-exposed host with auth enabled but not configured.
- `ENVIRONMENT=prod` without a configured Flask secret key.
- `on-prem-hardened` without a TLS boundary.
- `on-prem-hardened` without an IP allowlist or equivalent network ACL.

## Smoke checks

After startup, check the profile without exposing secrets:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/deployment/security-profile
```

Expected local-only result:

- `profile`: `local-only`
- `network_exposed`: `false`
- `status`: `safe`
- `active_controls` includes `loopback`

For LAN or on-prem profiles, confirm that the returned controls match the
approved deployment design before any user testing.

## Approval boundary

The following actions are outside this runbook and require explicit approval:

- Windows service or Task Scheduler installation.
- Firewall rule changes.
- Certificate creation/import.
- Reverse proxy installation or persistent proxy startup.
- DB/ODI live operation.
- Credential rotation or password changes.
