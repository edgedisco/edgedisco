# Production hardening checklist

The included implementation is suitable for an evaluation or controlled pilot. Complete the following work before broad enterprise deployment.

## Identity and access

- Replace the administrator token with OIDC or SAML SSO.
- Add scoped viewer, auditor, operator, and administrator roles.
- Add device-token revocation, enrollment expiry, and rotation.
- Consider mutual TLS or managed device certificates.

## Data and availability

- Replace local SQLite with managed PostgreSQL for multi-instance deployment.
- Define evidence retention and deletion schedules.
- Encrypt database storage and backups with managed keys.
- Test restore procedures and document recovery objectives.
- Add tenant isolation if the service will host more than one organization.

## Network and application security

- Terminate TLS at an approved ingress and redirect HTTP.
- Apply request-rate limits and body-size limits at ingress.
- Store server credentials in a secret manager.
- Add security headers at ingress and set session cookies as Secure.
- Forward authentication, enrollment, upload, export, and administrative audit events to a SIEM.
- Keep MCP on loopback; for remote use, require an authenticated HTTPS proxy that rewrites the
  upstream Host header, rate-limits requests, and records caller identity.
- Run the MCP process and database under a dedicated operating system account when untrusted local
  users or processes share the host.

## Endpoint distribution

- Build signed and notarized macOS packages.
- Sign Windows packages and distribute through Intune or SCCM.
- Publish checksums and verify them during installation.
- Deploy with MDM and monitor service health and version drift.
- Protect local device credentials from non-administrator users.
- Run collectors in each target user's context; do not use root, LocalSystem, or a macOS LaunchDaemon for current-user discovery.

## Privacy and workforce process

- Complete privacy, legal, HR, and works-council review as applicable.
- Document lawful purpose, data fields, access, retention, and employee notice.
- Validate that local collection rules match the published notice.
- Review new detection signatures for false positives and unnecessary data.
- Revalidate executable names, package markers, and standard install locations against vendor releases on a regular cadence.
- Review and sign static fingerprint-library updates; record release, distribution, platform, architecture, digest provenance, and review date.
- Replace low-entropy path, command, workspace, and user SHA-256 values with a versioned per-device or per-tenant HMAC-SHA-256 design before treating them as anonymized identifiers.

## Operational readiness

- Define approved, tolerated, and prohibited AI asset categories.
- Route findings into the existing endpoint, CMDB, SIEM, or case-management workflow.
- Establish ownership and response SLAs for newly observed assets.
- Load-test ingest volume and dashboard queries at the expected fleet size.
- Define MCP change-log and audit-log retention, cursor expiry, resnapshot procedures, and rotation.
- Do not treat the OTLP outbox and encoder as collector delivery. Before enabling OTLP network
  export, implement and test worker claiming, TLS/authentication, timeouts, partial success,
  retry/backoff, crash recovery, delivered-row retention, and queue health monitoring according to
  the [exporter specification](otel-integration.md#exporter-specification).
- Run an independent security review before production approval.
