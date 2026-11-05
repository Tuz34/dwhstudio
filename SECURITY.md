# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities through GitHub private vulnerability reporting
(Security tab, "Report a vulnerability") on this repository. If that is not available to you,
open a GitHub issue that describes the affected area without exploit details or secrets, and a
maintainer will move the conversation to a private channel. Do not post credentials, connection
strings, or real data in any report.

## Supported deployment model

DWH Studio is local-only by default. Do not expose a deployment to a network until its
authentication, session key, TLS termination and accepted authorities are verified. A configured
flag alone is not evidence of an encrypted connection. See
`app/odi-dwh/docs/deployment-security-profiles.md` for the source-version-specific controls.

## Remediation status (2026-11-05)

Numbers below identify the findings from the 2026-11-04 static review. These are source-code
remediation states, not a claim that every running installation is updated, that a live Oracle/ODI
rollback has been exercised, or that the original scan's UI records have been closed.

| Findings | Boundary | Source status / evidence |
|---|---|---|
| 1 | Placeholder signing keys | Merged in PRs [2](https://github.com/Tuz34/dwhstudio/pull/2) and [4](https://github.com/Tuz34/dwhstudio/pull/4); independently reverified. |
| 2 | Public status probe resource limits | [PR30](https://github.com/Tuz34/dwhstudio/pull/30) merged after independent review and CI. |
| 3, 7 | Preview schema restriction; mapping replacement order | [PR12](https://github.com/Tuz34/dwhstudio/pull/12) merged; independently reverified. |
| 4, 8 | Server-owned post-load gates; atomic append/watermark | [PR10](https://github.com/Tuz34/dwhstudio/pull/10) merged; independently reverified. |
| 5, 9, 12 | Request-local AI provider; credentialed egress; prompt/error handling | [PR36](https://github.com/Tuz34/dwhstudio/pull/36) merged after independent review, reviewed mapped-IPv6 correction and green CI. |
| 6, 10 | TLS transport and exact Host boundary | [PR32](https://github.com/Tuz34/dwhstudio/pull/32) merged after independent review and CI. |
| 11, 13 | Partial Oracle DDL reporting; unsupported DQ rules | [PR3](https://github.com/Tuz34/dwhstudio/pull/3) merged; independently reverified. |
| 14 | Metadata-sync success evidence | Earlier failure handling merged in PRs2/4; independent recheck found missing positive commit evidence. [PR33](https://github.com/Tuz34/dwhstudio/pull/33) closes that residual after independent review and CI. |

The frontend dependency follow-up, ODI-82, is merged in
[PR31](https://github.com/Tuz34/dwhstudio/pull/31). The verification-time npm audit reported zero
vulnerabilities including development dependencies. Dependency audit, lint, tests and build now
gate CI, and third-party Actions are pinned to reviewed upstream commit SHAs. An audit is a dated
advisory check, not proof that dependencies contain no vulnerabilities.

## Deployment limits and open work

- Until all relevant fixes are deployed and deployment checks pass, keep the instance local and
  single-user; do not expose AI configuration or credentials to untrusted callers. Do not put
  secrets or production rows in model prompts. Existing log/state contents are not retroactively
  sanitized by these logging changes.
- `APP_ALLOWED_ORIGINS` and `APP_IP_ALLOWLIST` must not be treated as enforced access controls merely
  because they appear in configuration or a validation report. Enforce network restrictions at a
  verified firewall/reverse proxy boundary where required.
- Network profiles require direct TLS or a trusted loopback TLS-terminating proxy, plus explicit
  accepted authorities. Proxy mode trusts the configured local-process boundary, not arbitrary
  forwarded headers from network peers. Operators must verify termination and header stripping;
  source tests do not attest the deployed proxy. Local-only HTTP requires a loopback bind and peer.
- Status-probe caching and rate limits are process-local. Multiple workers require an aggregate
  reverse-proxy limit; permanently hung underlying probes can require process restart. See
  `app/odi-dwh/docs/status-probe-security.md`.
- Oracle DDL may commit before a later statement fails. Partial-state reporting is not automatic
  rollback. Maintain backups, inspect actual state and obtain the required approval before recovery.
- Isolated tests and source review do not replace deployment-specific TLS, proxy, privilege,
  backup/restore or ODI runtime validation. Preserve explicit write approvals and scope gates.

## AI endpoint administration

The merged AI boundary in [PR36](https://github.com/Tuz34/dwhstudio/pull/36) requires
deployment of that source version before these controls can be relied on:

- `AI_TRUSTED_ORIGINS` is an administrator-owned, comma-separated list of exact HTTPS origins
  (scheme, hostname and port), not wildcard, path or API-request-controlled trust. Remote DNS
  names must resolve entirely to public addresses; a private service requires an explicitly
  trusted IP origin. The Claude cloud client also permits its fixed official API origin.
- Canonical loopback IP addresses and `localhost` are permitted by default at valid HTTP/HTTPS
  ports. `localhost` is pinned to IPv4 loopback. IPv4-mapped IPv6 forms are rejected explicitly.
  Outbound connections pin the validated address while retaining TLS hostname verification;
  redirects and ambient proxy settings are not followed.
- For local OpenAI-compatible configuration and CLI clients, a new origin cannot silently inherit
  a retained nonempty API key, including fallback or CLI defaults. Explicit credential
  replacement/rebinding or explicit empty clearing is required. An explicit empty key suppresses
  fallback; same-origin path changes retain the binding. These key-clearing/binding guarantees
  do not apply to Anthropic credentials: administrators must separately review the permitted
  `ANTHROPIC_BASE_URL` and its resolved key; an empty configuration update does not clear that key.
- Provider selection is request-local. Persisted benchmark/analyst diagnostics contain metadata,
  not prompts, generated analysis or provider error bodies. Successful transient responses and
  deliberately requested synthetic evaluation captures are separate from diagnostic persistence.

These controls do not make an untrusted provider safe to receive sensitive prompts and do not
sanitize historical logs. Review existing records separately without publishing their contents.

## Secrets and data

Generate signing keys with a cryptographically secure random source (at least 32 bytes of
randomness). The application's minimum-length, placeholder and simple-pattern checks reject
obvious weak values; they cannot prove a supplied key is unpredictable. Network profiles fail
closed when screening fails. Local-only mode uses a fresh temporary runtime key instead, so
sessions from the rejected value no longer validate; sessions also expire across those restarts.

Never commit `.env*` files or `odi_connection.properties`. Only the `*.example` templates belong in
the repository. Evidence and log output are designed to carry counts and status, not raw rows.
