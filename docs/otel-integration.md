# OpenTelemetry integration

EdgeDisco projects privacy-filtered asset state changes into a durable SQLite outbox and
exports them as OTLP Logs over HTTP with binary protobuf. Delivery runs in a separate process,
so collector outages do not block inventory ingestion.

The exporter is a separate Python process, not a thread or task inside the inventory server.
Managed installations supervise it with launchd on macOS or `systemd --user` on Linux. The two
processes coordinate only through the SQLite outbox: the server writes state changes and the
exporter claims and completes them using expiring leases.
Within the exporter process, each HTTP request runs in a helper thread so its main loop can enforce
the request deadline and shutdown grace period. No exporter thread runs inside the inventory server.

```mermaid
flowchart LR
  A[Accepted inventory snapshot] --> P[Strict asset projection]
  P -->|state changed| Q[SQLite OTLP outbox]
  Q --> E[OTLP Logs protobuf exporter]
  E --> C[OpenTelemetry Collector]
  C --> L[Loki]
```

## Managed setup paths

Use the operating-system guide for complete installation, enablement, verification, pause, and
disablement steps:

- [macOS OTLP setup](deployment-macos.md#5-set-up-optional-otlp-export)
- [Linux OTLP setup](deployment-linux.md#5-set-up-optional-otlp-export)

There is no dashboard control for OTLP. Managed installations read settings from
`~/.edgedisco/server.env`; both switches default to `false`, and no exporter service exists until
delivery is explicitly enabled. The rest of this page is the exporter configuration and behavior
reference.

The two switches support three modes:

| Outbox | Export | Behavior |
| --- | --- | --- |
| `false` | `false` | Fully off. No new OTLP records and no exporter service. This is the default. |
| `true` | `false` | Queue-only. New state changes enter the bounded outbox, but setup removes the exporter service and sends nothing. |
| `true` | `true` | Active. New state changes enter the outbox and the separate exporter service delivers due records. |

`false`/`true` is invalid because delivery cannot run without the outbox. Setup removes the
exporter service only after it verifies that the process stopped. Disabling OTLP does not delete
existing outbox rows or delivery totals. `edgedisco uninstall --purge --yes` deletes the complete
managed database; there is no OTLP-only purge command.

For the default managed database, `edgedisco otlp-status --json` automatically reads
`~/.edgedisco/server.env`. For a custom root, pass both `--db` and `--env-file`. Use
`--process-env` only for a manual process configured by the current shell.

## Test against the local stack

With the Docker Compose stack in `../otel-stack` running, use
`http://127.0.0.1:4318/v1/logs` as the collector endpoint. Port `4318` is OTLP ingestion, port
`3001` is Grafana, and port `3100` is Loki.

For a managed installation, follow the macOS or Linux setup link above and use that endpoint. To
test the pipeline from a source checkout without sending real endpoint inventory, create a
development environment and run the isolated verification script:

```sh
cd /path/to/edgedisco
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[otlp]'
python scripts/verify_otlp_export.py \
  --endpoint http://127.0.0.1:4318/v1/logs \
  --output-dir /tmp/edgedisco-otel-verification
```

Use a fresh output directory on each run. The script creates its own database, invokes the real
exporter, alternates plain and gzip requests, and queries Loki. It checks running and stopped
transitions, versions, simulation and host metadata, observation IDs, and event and receipt
timestamps. It leaves the test database and `result.json` in the output directory and prints a
LogQL query for Grafana Explore. No real endpoint inventory is sent.

For any manual foreground exporter, shell variables configure only processes started from that
shell; they do not create or remove a managed service. `edgedisco otlp-export --once` processes one
due claim and exits, while the normal command polls until SIGINT or SIGTERM. The worker requires an
existing database and never creates or replaces one.

The projection emits `edgedisco.asset.observed` log records for recognized applications,
processes, and agent runtimes. It exports state transitions rather than every heartbeat. The
allowlisted attributes are schema version, observation ID, device ID, asset kind, name, vendor,
optional version, running state, simulation marker, and validated agent host/relationship fields.
MCP configuration is intentionally excluded from OTLP because server names are user-controlled;
the sanitized MCP synchronization feed is available through MCP instead.

Raw paths, path hashes, command hashes, binary hashes, arbitrary metadata, prompts, responses,
credentials, arguments, environment values, and configuration URLs never enter the OTLP outbox.
The encoder validates the stored JSON again before creating protobuf bytes. It maps the endpoint's
inventory observation time to `time_unix_nano` and the server receipt time to
`observed_time_unix_nano`. Queued records from releases before receipt time was stored remain
encodable and use the inventory observation time for both fields.

## Queue behavior

The default queue holds at most 5,000 undelivered records (including active claims), 16 MiB of
undelivered JSON, and seven days of undelivered evidence. A newer state for the same logical asset
supersedes its older pending state. Age, capacity, oversize, and superseded drops increment
`dropped_events_total`; status is
available through `edgedisco otlp-status` and `Database.otlp_outbox_status()`. Local inventory
ingestion continues if projection bookkeeping fails or outbound evidence is dropped.
When a failed request or expired lease returns an older in-flight record to retry, the worker
checks the latest observation ID and discards superseded records. This also applies after a newer
delivered row has been cleaned up. Requests already sent cannot be recalled; consumers should use
event timestamps rather than arrival order when deriving current state.

This is a latest-state delivery design. It does not guarantee delivery of every intermediate
transition when the destination is unavailable. The worker removes delivered rows after one day
and failed rows after seven days by default. Cleanup and age eviction use bounded transactions.

## Exporter contract

### Scope and process model

The first exporter release supports OTLP Logs over HTTP with binary protobuf. JSON and OTLP/gRPC
are outside the first release. Export runs as a separately supervised process:

```text
edgedisco otlp-export --db /path/to/inventory.db
```

Keeping delivery separate from the inventory HTTP server prevents collector latency, TLS failures,
and exporter restarts from delaying inventory ingestion. A managed installation adds the exporter
service only when delivery is enabled. Multiple exporter processes may point at the same database;
the lease protocol below ensures that each row has one active owner.

Outbox creation and network delivery are separate switches. This permits validation of projected
events before allowing egress:

```dotenv
EDGEDISCO_OTLP_OUTBOX_ENABLED=true
EDGEDISCO_OTLP_EXPORT_ENABLED=true
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=https://collector.example.com/v1/logs
OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/protobuf
```

`EDGEDISCO_OTLP_EXPORT_ENABLED=true` requires `EDGEDISCO_OTLP_OUTBOX_ENABLED=true` and an explicit
endpoint. EdgeDisco must not silently use the OpenTelemetry SDK default of localhost because an
operator should make telemetry egress deliberate.

### Configuration contract

Use the standard OpenTelemetry exporter variable names wherever the OpenTelemetry specification
defines one. Signal-specific variables take precedence over their general equivalents.

| Setting | Default | Contract |
| --- | --- | --- |
| `EDGEDISCO_OTLP_OUTBOX_ENABLED` | `false` | Project state changes into the durable outbox. |
| `EDGEDISCO_OTLP_EXPORT_ENABLED` | `false` | Start network delivery. Requires the outbox and an endpoint. |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | unset | Exact Logs URL. No path is appended. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Base URL; append `/v1/logs`, preserving any path prefix. |
| `OTEL_EXPORTER_OTLP_LOGS_PROTOCOL` | `http/protobuf` | Must be `http/protobuf` in the first release. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | General protocol, used only when the Logs protocol is unset. |
| `OTEL_EXPORTER_OTLP_LOGS_HEADERS` | unset | Comma-separated `key=value` request headers. |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | General headers, used only when Logs headers are unset. |
| `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` | `10000` | Whole-request timeout in milliseconds. |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `10000` | General timeout, used only when the Logs timeout is unset. |
| `OTEL_EXPORTER_OTLP_LOGS_COMPRESSION` | unset | `gzip` or no compression. |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | unset | General compression, used only when Logs compression is unset. |
| `OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE` | system trust | PEM CA certificate path for server verification. |
| `OTEL_EXPORTER_OTLP_CERTIFICATE` | system trust | General CA path, used only when the Logs CA is unset. |
| `OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE` | unset | PEM client certificate path; requires the client key. |
| `OTEL_EXPORTER_OTLP_LOGS_CLIENT_KEY` | unset | PEM client key path; requires the client certificate. |
| `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` | unset | General client certificate, used only when its Logs setting is unset. |
| `OTEL_EXPORTER_OTLP_CLIENT_KEY` | unset | General client key, used only when its Logs setting is unset. |
| `EDGEDISCO_OTLP_BATCH_RECORDS` | `100` | Maximum records claimed per request. Range 1-1000. |
| `EDGEDISCO_OTLP_BATCH_BYTES` | `1048576` | Maximum uncompressed protobuf request size. |
| `EDGEDISCO_OTLP_POLL_INTERVAL_MS` | `1000` | Delay after a scan finds no due work. |
| `EDGEDISCO_OTLP_LEASE_SECONDS` | `60` | Claim lifetime; must exceed the request timeout. |
| `EDGEDISCO_OTLP_SHUTDOWN_GRACE_SECONDS` | `15` | Time allowed for the active request during shutdown. |
| `EDGEDISCO_OTLP_DELIVERED_RETENTION_DAYS` | `1` | Retention for successfully delivered rows. |
| `EDGEDISCO_OTLP_FAILED_RETENTION_DAYS` | `7` | Retention for terminal failure diagnostics. |

The endpoint rules are:

- `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` is used exactly as configured.
- Otherwise, append `v1/logs` to `OTEL_EXPORTER_OTLP_ENDPOINT`. For example,
  `https://host/otel` becomes `https://host/otel/v1/logs`.
- Reject URLs containing user information, a query, or a fragment. Reject schemes other than
  `https`, except that `http` is allowed for a literal loopback address.
- Do not follow redirects or use ambient HTTP proxy variables. A proxy changes the telemetry trust
  boundary and needs a future explicit setting.

Parse header values according to the OpenTelemetry key/value header format. Reject malformed or
duplicate entries and headers owned by the transport, including `content-type`, `content-length`,
`content-encoding`, `host`, `connection`, and `user-agent`. Never log header values, certificate
contents, private-key paths, or URLs containing credentials.

Configuration is validated before the worker opens the database. Invalid combinations fail startup
with a message naming the setting, but without reproducing a secret value. The worker reloads no
settings at runtime; changing endpoint or credentials requires a supervised restart.

### Durable state and claiming

Schema version 3 extends the outbox `status` check with `sending` and adds these nullable columns:

| Column | Meaning |
| --- | --- |
| `lease_id` | Random identifier for the current claim; null outside `sending`. |
| `lease_expires_at` | UTC claim expiry; null outside `sending`. |
| `last_attempt_at` | UTC time at which the most recent HTTP attempt began. |
| `failed_at` | UTC time at which the row entered terminal failure. |
| `last_http_status` | Most recent HTTP status, or null when no response was received. |

Existing `attempt_count`, `next_attempt_at`, `delivered_at`, and `last_error_code` columns remain.
Limit error categories to a fixed enum and never store a response body or rendered request payload.
`otlp_export_status` includes cumulative attempted, retried, delivered, and failed event counts,
plus `last_attempt_at`, `last_success_at`, `last_failure_at`, `last_failure_code`, and
`last_http_status`. These aggregates survive row cleanup.

The state machine is:

```mermaid
stateDiagram-v2
  [*] --> pending
  pending --> sending: atomic claim
  retry --> sending: due and atomic claim
  sending --> delivered: complete success
  sending --> retry: retryable failure
  sending --> failed: permanent failure
  sending --> retry: lease expires
```

Claim rows in a short `BEGIN IMMEDIATE` transaction. First recover expired `sending` rows to
`retry`, then select due `pending` and `retry` rows in stable insertion order, limited by record
count and estimated batch bytes. Set one new cryptographically random `lease_id`, `sending`, and
lease expiry, then commit. Increment attempt count and set `last_attempt_at` immediately before each
HTTP request, after encoding and rechecking ownership. Perform encoding and network I/O only
after the transaction commits.

Every completion update must match both the row ID and `lease_id`. A worker whose lease expired
cannot overwrite the decision made by a new owner. Extend the lease before a request only when the
remaining lifetime cannot cover the configured request timeout and shutdown grace. Stop claiming
new work on `SIGTERM` or `SIGINT`; allow the active request to finish for the configured grace
period, then leave its rows for lease recovery.

### Encoding and request behavior

Validate each stored projection again and encode one batch as one
`ExportLogsServiceRequest`. A row that cannot be validated or encoded moves to `failed` with an
`invalid_payload` category; valid rows from the same claim continue in a smaller request. Build
batches without exceeding either configured limit. If one valid record exceeds the byte limit,
send it alone subject to the existing outbox hard limit.

POST the request with:

```http
Content-Type: application/x-protobuf
User-Agent: edgedisco/<version>
Content-Encoding: gzip  # only when configured
```

Reuse HTTP connections, verify server certificates and hostnames, and apply the timeout to the
complete request including reading the response. Cap response bodies at 4 MiB. Do not send
cookies. The observation ID remains stable across retries and is the receiver's available
deduplication key; OTLP itself does not provide exactly-once delivery.

### Response and retry policy

Classify each completed request as follows:

| Result | Action |
| --- | --- |
| HTTP 2xx with an empty or valid protobuf response and no rejected records | Mark the batch `delivered`. |
| HTTP 2xx with `partial_success.rejected_log_records > 0` | Mark the batch `failed` as `partial_success`. The protocol does not identify rejected rows, so do not retry any row from that batch. |
| HTTP 2xx with an invalid or oversized response | Retry as `invalid_response`. |
| Connection failure, disconnect, or timeout | Retry as `transport`. |
| HTTP 429, 502, 503, or 504 | Retry as `http_retryable`; honor a valid `Retry-After`. |
| Any other HTTP status | Mark the batch `failed` as `http_permanent`. |

An empty successful body is the zero-length encoding of an empty protobuf response and is valid.
Ignore a partial-success error message except for bounded in-memory debug logging; it may contain
collector-controlled text and must not be persisted.

Retry with full-jitter exponential backoff: choose a random delay from zero through
`min(60 seconds, 1 second * 2^(attempt-1))`. A valid `Retry-After` is the minimum delay, capped at
five minutes. Persist `next_attempt_at` so restarts do not reset backoff. There is no fixed retry
count; the existing outbox age and capacity limits bound retry retention. When a row becomes a
permanent failure, remove its latest-state deduplication entry if that entry still points to the
failed row, allowing a later observation to create a fresh event.

### Cleanup, status, and failure isolation

Periodically delete delivered and failed rows older than their configured retention. Cleanup uses
small transactions and never deletes `pending`, `retry`, or unexpired `sending` rows. Keep the
existing count and byte limits for undelivered rows.

Add `edgedisco otlp-status --db /path/to/inventory.db` with human-readable output and `--json`.
Status includes counts and bytes by state, oldest due age, expired leases, total retries and drops,
last attempt, last success, last permanent failure category, and exporter configuration validity.
It must not expose event JSON, endpoint headers, certificates, or other credentials.

Exporter initialization and runtime failures never stop the inventory service. Conversely, a
corrupt or unsupported database schema makes the exporter fail closed instead of recreating or
truncating the database. SQLite busy errors return claimed work to normal polling without tight
loops.

### Security and privacy invariants

The exporter may read only the OTLP outbox and its own status metadata. It must not rebuild events
from raw inventory tables. Before every send, validation enforces the same strict allowlist used at
projection time. Logs and metrics contain counts, durations, error categories, and HTTP status
codes only; they never contain payloads, device identifiers, asset names, endpoint secrets, or
collector response text.

Remote collectors require HTTPS. Plain HTTP is permitted only for literal loopback addresses
(`127.0.0.0/8` and `::1`), not hostnames that happen to resolve to loopback. CA and client-key files
must be regular files, must not be group/world writable, and the private key must not be
group/world readable. Authentication headers come from protected process configuration rather
than the inventory database.

### Reference collector: `otel-stack`

The companion `otel-stack` repository is the reference integration environment. Its collector
currently receives OTLP/HTTP on port 4318 and routes Logs through a memory limiter, an OTTL body
transform, batching, and Loki's native OTLP endpoint. A same-host development setup uses:

```dotenv
EDGEDISCO_OTLP_OUTBOX_ENABLED=true
EDGEDISCO_OTLP_EXPORT_ENABLED=true
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/protobuf
```

The current stack does not terminate TLS or authenticate collector requests. EdgeDisco may use it
directly only on the same host through a loopback address. Before a remote EdgeDisco instance
points at it, place an authenticated TLS ingress in front of the collector and restrict direct
access to ports 4317 and 4318.

The stack's transform and Loki metadata contract consumes the existing schema version,
observation ID, device ID, asset kind/name/vendor/running state, simulation marker, and optional
host application and relationship. It also preserves optional `asset.version`. The distinct
OTLP event and observed timestamps require no collector transform, but integration tests must
verify both survive ingestion. The repository integration script covers versioned and stopped
assets against the running stack.

### Verification coverage

The exporter tests and opt-in live integration cover:

1. Endpoint precedence, path construction, header parsing, TLS/mTLS validation, compression, and
   secret-safe configuration errors.
2. Atomic competing-worker claims, lease expiry recovery, stale-owner rejection, clean shutdown,
   and restart persistence.
3. Record/byte batch limits, malformed-row isolation, valid protobuf, stable observation IDs, and
   distinct event/observed timestamps.
4. Every response class above, including empty success, protobuf partial success, `Retry-After`,
   capped jitter, timeout, disconnect, invalid response, and response-size bounds.
5. Delivered and failed retention, outbox age/capacity behavior, permanent-failure dedup recovery,
   status accuracy, and the absence of payloads or secrets from logs.
6. An end-to-end run against `otel-stack` that exports a running asset, a stopped transition, a
   versioned asset, and a simulated asset, then queries Loki to verify the expected metadata and
   timestamps.

OTLP configuration follows the [OpenTelemetry exporter specification](https://opentelemetry.io/docs/specs/otel/protocol/exporter/).
The worker deliberately requires explicit enablement and an endpoint, and restricts plain HTTP
to literal loopback addresses. It does not support OTLP/gRPC, JSON delivery, or remote HTTP.
