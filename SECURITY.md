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
| 5, 9, 12 | Request-local AI provider; credentialed egress; prompt/error handling | Implementation candidate undergoing independent review; not yet a merged remedy. |
| 6, 10 | TLS transport and exact Host boundary | [PR32](https://github.com/Tuz34/dwhstudio/pull/32) merged after independent review and CI. |
| 11, 13 | Partial Oracle DDL reporting; unsupported DQ rules | [PR3](https://github.com/Tuz34/dwhstudio/pull/3) merged; independently reverified. |
| 14 | Metadata-sync success evidence | Earlier failure handling merged in PRs2/4; independent recheck found missing positive commit evidence. [PR33](https://github.com/Tuz34/dwhstudio/pull/33) closes that residual after independent review and CI. |

The frontend dependency follow-up, ODI-82, is merged in
[PR31](https://github.com/Tuz34/dwhstudio/pull/31). The verification-time npm audit reported zero
vulnerabilities including development dependencies. Dependency audit, lint, tests and build now
gate CI, and third-party Actions are pinned to reviewed upstream commit SHAs. An audit is a dated
advisory check, not proof that dependencies contain no vulnerabilities.

## Deployment limits and open work

- Until the AI remedies above are merged and all relevant fixes are deployed, keep the instance local and
  single-user; do not expose AI configuration or credentials to untrusted callers. Do not put
  secrets or production rows in model prompts. Existing log/state contents are not retroactively
  sanitized by a future logging change.
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

## Secrets and data

Never commit `.env*` files or `odi_connection.properties`. Only the `*.example` templates belong in
the repository. Evidence and log output are designed to carry counts and status, not raw rows.
