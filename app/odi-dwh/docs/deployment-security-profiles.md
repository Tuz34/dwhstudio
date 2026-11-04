# Deployment Security Profiles

This runbook defines how the local DWH Studio app may be exposed. It is a
configuration and review guide. It does not install a service, change
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
`0.0.0.0`. HTTP remains supported. Requests must also have a literal loopback
socket peer (`127.0.0.1` or `::1`) and an allowed Host authority, even if an
external WSGI launcher overrides the configured bind address.

### lan-pilot

Controlled pilot profile for a trusted internal network segment.

- `DEPLOYMENT_PROFILE=lan-pilot`
- `APP_HOST=0.0.0.0` only with explicit approval.
- `APP_DEBUG=false`
- `APP_AUTH_ENABLED=true`
- `APP_AUTH_USERNAME` and `APP_AUTH_PASSWORD_HASH` must be configured.
- `APP_IP_ALLOWLIST` should be set to the approved network segment or host list.
- A working TLS boundary is mandatory, including for single-operator pilots.
- Configure explicit `APP_ALLOWED_HOSTS` authorities and one transport below.
- `FLASK_SECRET_KEY` must be configured with a non-placeholder value.

This profile is review-required. It is not a hardened institutional deployment.

### on-prem-hardened

Target profile for institutional/on-prem use.

- `DEPLOYMENT_PROFILE=on-prem-hardened`
- `APP_DEBUG=false`
- `APP_AUTH_ENABLED=true`
- `APP_AUTH_USERNAME` and `APP_AUTH_PASSWORD_HASH` must be configured.
- `FLASK_SECRET_KEY` must be configured with a non-placeholder value.
- TLS must be provided through exactly one of the transport modes below.
- Configure explicit `APP_ALLOWED_HOSTS` authorities.
- `APP_IP_ALLOWLIST` must be set or an equivalent firewall/network ACL must be
  documented by the operator.
- Allowed browser origins should be listed in `APP_ALLOWED_ORIGINS` when the app
  is accessed through a named host.

## Transport modes

TLS flags are not evidence that the deployed listener encrypts traffic. Startup
and request-time checks enforce the selected boundary; deployment verification
of certificates, reverse-proxy configuration and firewall policy is still required.

### Direct TLS

Set `APP_TLS_ENABLED=true`, `APP_REVERSE_PROXY_TLS_ENABLED=false`,
`APP_TLS_CERT_FILE` and `APP_TLS_KEY_FILE` to operator-provisioned certificate and
key paths. Startup must successfully load the pair into a server SSL context
(minimum TLS 1.2) before accepting requests. Missing, unreadable or invalid
material aborts startup with a fixed error; paths and underlying errors are not
returned. No certificate is generated automatically.

The `python app.py` entry point passes this context to `app.run`; the flag alone
never enables TLS. Flask's built-in server is for controlled development/pilot
use, not a production server recommendation. An external WSGI server must itself
provide TLS and set `wsgi.url_scheme=https` from its transport. Plain HTTP requests
are rejected before auth/routes, even when carrying `X-Forwarded-Proto: https`.
Do not add a generic `ProxyFix` middleware or rewrite transport/peer metadata from
untrusted headers. External launchers must preserve the configured debug-off
policy; do not use Flask CLI overrides to change deployment security settings.

### Loopback TLS-terminating reverse proxy

Set `APP_REVERSE_PROXY_TLS_ENABLED=true`, `APP_TLS_ENABLED=false` and
`APP_HOST=127.0.0.1` (or `::1` / `localhost`). Both network profiles still require
configured auth, a session key and debug disabled despite this loopback bind.
The application listener is HTTP on loopback only; a wildcard/LAN bind is rejected.

The trusted local proxy must terminate TLS, reject or redirect external HTTP
before forwarding credentials, preserve the approved Host authority, strip
client-supplied forwarding headers, and set one exact `X-Forwarded-Proto: https`
only for requests received over TLS. The app accepts that assertion only from
the raw socket peer `127.0.0.1` or `::1`; missing, combined or non-HTTPS assertions
fail closed. `X-Forwarded-For`, `X-Forwarded-Host` and `Forwarded` do not change the
peer or authority used by the guard. Only after the trusted-peer/HTTPS check does
the guard set the downstream WSGI scheme to HTTPS, preserving Flask's secure URL
and CSRF behavior. The trusted local-process boundary cannot
distinguish a legitimate proxy from another process on the same machine; remote
proxy peers are deliberately unsupported. Verify the actual external TLS
listener separately: a configuration flag cannot attest to proxy behavior.

## Exact Host authority policy

A WSGI guard executes before Flask dispatch, auth, CSRF, rate limiting and all
public/static routes. It does not bypass any of those existing controls for valid
requests. Host is mandatory and must be an exact parsed authority, not a URL.
No DNS resolution, wildcard, prefix or suffix matching is performed.

The default authorities are `localhost`, `127.0.0.1` and bracketed IPv6 `[::1]`,
on the configured `APP_PORT` or the selected transport's default port (80/443).
DNS names are case-insensitive; IPv6 literals use address equivalence. Trailing
dots, unbracketed IPv6, zone identifiers, userinfo, whitespace, commas and invalid
ports are rejected. Noncanonical IPv4 spellings are not loopback aliases.

Network profiles additionally require an explicit comma-separated
`APP_ALLOWED_HOSTS`, for example `studio.example.test,studio.example.test:8443`.
An omitted port means 443 for TLS modes; a nondefault port must be listed exactly.
Explicit IPv4 and bracketed IPv6 authorities are supported. `APP_HOST` and
`APP_PUBLIC_HOST` do not implicitly authorize a network Host. Local-only cannot
add non-loopback names. The Host policy is not a client-IP ACL or a CORS policy.

`APP_IP_ALLOWLIST` and `APP_ALLOWED_ORIGINS` remain report-only configuration.
They are not enforced by the application and are not listed as active controls.
The profile reports `ip_allowlist_enforced=false` and
`allowed_origins_enforced=false`; operators must implement required network ACLs
and browser-origin policy separately. Network profile status remains
`review-required`, not proof of a hardened installation.

## Blocked combinations

The Flask startup validator blocks these combinations:

- `local-only` with a network-wide host.
- Either network profile with `APP_DEBUG=true`, including behind a loopback proxy.
- Either network profile without configured, enabled auth.
- `ENVIRONMENT=prod` without a configured Flask secret key.
- Either network profile without a TLS boundary or explicit allowed authorities.
- Both TLS modes enabled, direct TLS without a loadable certificate/key, or a
  reverse proxy profile with a non-loopback app bind.
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
approved deployment design through the configured HTTPS authority, not a direct
HTTP probe. Check rejected Host and cleartext cases as well as successful TLS.
Never treat configured allowlist/origin counts as evidence of enforcement.

## Approval boundary

The following actions are outside this runbook and require explicit approval:

- Windows service or Task Scheduler installation.
- Firewall rule changes.
- Certificate creation/import.
- Reverse proxy installation or persistent proxy startup.
- DB/ODI live operation.
- Credential rotation or password changes.
