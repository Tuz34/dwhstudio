# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities through GitHub private vulnerability reporting
(Security tab, "Report a vulnerability") on this repository. If that is not available to you,
open a GitHub issue that describes the affected area without exploit details or secrets, and a
maintainer will move the conversation to a private channel. Do not post credentials, connection
strings, or real data in any report.

## Supported deployment model

DWH Studio is local-only by default. Network profiles (`lan-pilot`, `on-prem-hardened`) require TLS
termination (directly or through a reverse proxy) and a configured session key. See
`app/odi-dwh/docs/deployment-security-profiles.md`.

## Known limitations

These items come from a static security review dated 2026-11-04. They are listed by title with the
current mitigation stance. Numbers refer to the review.

**(2) Public status endpoint fans out expensive probes without a limit.** The public status route
runs several backend probes per request and does not rate-limit or cache them, so a client that can
reach the port can cause repeated load on the database and ODI checks. Mitigation: keep the
`local-only` profile or restrict access with the IP allowlist and a reverse proxy rate limit until
caching and throttling are added.

**(5) Temporary provider override can route another user's prompt to a cloud model.** A request can
temporarily switch the AI provider, and the switch is not scoped to the requesting user. Another
user's prompt could therefore be sent to a cloud model even when the operator intended local-only
inference. Mitigation: run single-user, leave `ANTHROPIC_API_KEY` empty if cloud inference is not
wanted, and do not share one instance between users with different data-handling expectations.

**(6) LAN profile allows password and session traffic without TLS.** The `lan-pilot` profile is
marked review-required and does not force a TLS boundary, so login passwords and session cookies can
cross the network in clear text. Mitigation: do not use `lan-pilot` on untrusted networks; put a TLS
reverse proxy in front of it or use `on-prem-hardened`, which requires a TLS boundary.

**(9) Operator-controlled model URL can redirect credentialed server requests.** The configured model
endpoint URLs are used for server-side requests that can carry credentials. An operator, or anyone
able to change those settings, can point them at another host and receive those credentials.
Mitigation: treat model URL settings as administrator-only, keep them on `localhost` unless you
control the target, and do not expose configuration to untrusted users.

**(10) Local unauthenticated API has no Host boundary.** In the default local-only mode the API has
no authentication and does not validate the `Host` header, so a malicious web page could attempt
DNS-rebinding style requests against a locally running instance. Mitigation: enable authentication,
set `APP_ALLOWED_ORIGINS`, and avoid browsing untrusted sites while the app is running.

**(12) Orchestration stores raw prompts and returns raw exception text.** The AI orchestration layer
persists the prompts it receives and some error responses include the underlying exception message,
which can contain table names, SQL fragments, or other internal detail. Mitigation: do not put
secrets in prompts, protect the log and state files with filesystem permissions, and do not expose
the API to untrusted clients.

## Fixed before publication

Addressed in the `fix/sec-01-14` and `fix/sec-11-13` branches (not yet merged at the time of writing):

- (1) Placeholder session key
- (11) Partial DDL reporting
- (13) Unsupported quality rules
- (14) Groovy failure reporting

## Planned

- (3) Source preview schema restriction
- (4) Caller-asserted post-load gates
- (7) Mapping replacement ordering
- (8) Non-atomic incremental append and watermark update

Items (7) and (8) are part of a broader transaction and atomicity hardening of the ODI and Oracle
execution paths, which is the main post-publication roadmap item.

## Secrets and data

Never commit `.env*` files or `odi_connection.properties`. Only the `*.example` templates belong in
the repository. Evidence and log output are designed to carry counts and status, not raw rows.
