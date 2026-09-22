# Security review: inventory lifecycle and telemetry

Date: 2026-09-21. This is a source and regression-test review, not an independent penetration
test or a certification that the repository has no vulnerabilities.

## Scope

Reviewed the changed scan reconciliation, SQLite migration, dashboard refresh, MCP membership,
OTLP projection/encoding/queue behavior, and the adjacent agent upload, server authentication and
validation, exporter transport, and CI permissions. The review focused on credential handling,
untrusted input, stale evidence, data disclosure, and delivery integrity.

## Findings addressed

| Finding | Fix and regression coverage |
| --- | --- |
| Agent uploads used the default redirect handler, which could forward bearer credentials after an HTTP redirect. | Reject redirects for authenticated enrollment, inventory, and runtime uploads. A local HTTP regression test checks that the redirected target receives no request. Configure the final server URL. |
| Receipt of an old scan could make old evidence appear fresh. | Require recent observation and receipt times. Delayed-scan tests retain historical running evidence while the UI/API classify it as stale. |
| Presence was conflated with running; missing stopped evidence could generate no change. | Derive presence on the server from full snapshots. Test stop, removal, reappearance, legacy unknown presence, and rejection of endpoint-supplied presence. |
| Multiple retained fingerprints could project an obsolete version. | Prefer current presence, then running, then latest observation; test replacement, reopening, and removal. |
| Dashboard state persisted without refresh or session-expiry handling. | Poll without overlapping requests, clear evidence on authorization failure, and label refresh failures. Test hidden-tab behavior, failures, expiry, and HTML escaping. |

## Boundaries checked

- Device identity comes from the bearer token, not a report field. SQL updates remain scoped to
  that device, with parameterized values and a single write transaction for reconciliation.
- OTLP heartbeat counts are computed by the server. The encoder rejects cross-event fields,
  arbitrary attributes, invalid flags/counts, inconsistent presence/running, and out-of-range
  protobuf timestamps. Legacy schema-1 records remain encodable without inventing presence.
- Heartbeats share existing queue bounds, coalescing, and leases. Duplicate and older scans do
  not emit new current-state events. This deliberately does not guarantee all transitions.
- Dashboard evidence uses HTML escaping, refresh errors do not expose exception details, and
  inventory responses are authenticated and marked no-store.
- OTLP transport retains explicit destination configuration, HTTPS for remote destinations,
  certificate verification, no redirects or ambient proxies, bounded requests, and safe errors.
- CI uses read-only repository permissions and now includes lifecycle/exporter tests with the
  optional dependencies installed.

## Remaining limitations

The built-in threaded HTTP server has no global connection/rate limit and is unsuitable for direct
untrusted Internet exposure. Use an authenticated TLS ingress with connection, timeout, and request
limits. Production SSO/RBAC, credential revocation/rotation, secure cookies at ingress, package
signing, retention, and comprehensive administrative auditing remain tracked in
[production hardening](production-hardening.md).

A valid device token authorizes reports, not their truth. An attacker holding it can send false
but schema-valid evidence, timestamps within the allowed clock tolerance, and allowed textual
values. Field allowlists reduce accidental collection; they do not prevent a compromised endpoint
from encoding sensitive content in an allowed version string. Do not describe this as attestation
or an absolute guarantee that malicious reports cannot contain secrets.

The collector/Loki pipeline is a separate trust boundary. Its consumers need schema-2
presence and device heartbeat support; the existing asset-only transform leaves the fixed heartbeat
body intact. The updated verification script delivered and verified 12 synthetic events through
the running local collector into Loki, covering stop, absence, reappearance, versions, device
heartbeats, gzip, and both timestamps.

**Separate `otel-stack` deployment boundary:** This review treats that Docker stack as
local-only and made no changes to its repository. Loki and collector authentication are disabled,
and Grafana enables anonymous admin access for local evaluation. Any explicit remote deployment
override requires restricted backend ports, authenticated TLS ingress, Grafana authentication,
and a non-default password.

This review did not include an external vulnerability advisory scan, fuzzing campaign, or
OS/package-signing assessment.
