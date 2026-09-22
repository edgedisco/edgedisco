# SPEC-001: EdgeDisco OTLP Projection, Invariants, and Traceability

**Status:** ✅ Active  
**Version:** 0.5.0  
**Schema Version:** 2  

---

## 1. Overview

This document specifies the formal behavioral requirements, privacy invariants, wire-format serialization contracts, and traceability matrix for EdgeDisco's OpenTelemetry (OTLP) projection and outbox delivery pipeline.

---

## 2. Formal Requirements Matrix (Traceability)

| Requirement ID | Statement | Spec Section | Fixtures / Tests | Implementation Paths | Verification Command | Status |
|---|---|---|---|---|---|---|
| `REQ-ED-001` | **Privacy Noninterference:** Outbox payloads, OTLP Logs records, exports, and API responses MUST NEVER contain prompt text, model responses, source code, credentials, environment variables, full filesystem paths, or raw CLI command tokens. | §3.1 | `privacy_canaries.json`<br>`test_privacy_invariants.py` | `src/ai_asset_inventory/otlp_events.py`<br>`src/ai_asset_inventory/otlp_encoder.py` | `pytest tests/test_privacy_invariants.py` | COVERED |
| `REQ-ED-002` | **Schema v2 Determinism:** Identical inventory evidence and monotonic timestamps produce identical observation digests (`sha256:<hex>`) and byte-for-byte matching OTLP Protobuf / JSON output. | §3.2, §4 | `observation_v2.pb`<br>`observation_v2.json`<br>`test_otlp_fixtures.py` | `src/ai_asset_inventory/otlp_events.py`<br>`src/ai_asset_inventory/otlp_encoder.py` | `pytest tests/test_otlp_fixtures.py` | COVERED |
| `REQ-ED-003` | **Outbox At-Least-Once Delivery & Idempotency:** Duplicate scans deduplicate into existing observation state; failed OTLP HTTP transmissions leave outbox records in retryable state without duplicate side-effects. | §3.3 | `test_outbox_invariants.py` | `src/ai_asset_inventory/otlp_store.py`<br>`src/ai_asset_inventory/otlp_exporter.py` | `pytest tests/test_outbox_invariants.py` | COVERED |
| `REQ-ED-004` | **Evidence Class Partitioning:** Installed, configured, running, observed, and simulated states remain strictly partitioned and cannot collapse into ambiguous boolean flags. | §3.4 | `test_detector.py`<br>`test_inventory_lifecycle.py` | `src/ai_asset_inventory/detector.py`<br>`src/ai_asset_inventory/models.py` | `pytest tests/test_inventory_lifecycle.py` | COVERED |
| `REQ-ED-005` | **Heartbeat Telemetry:** Periodic device inventory heartbeats emit `edgedisco.device.inventory` with exact total and simulated asset counts. | §4.2 | `device_inventory_v2.pb`<br>`device_inventory_v2.json`<br>`test_otlp_encoder.py` | `src/ai_asset_inventory/otlp_events.py`<br>`src/ai_asset_inventory/otlp_encoder.py` | `pytest tests/test_otlp_encoder.py` | COVERED |
| `REQ-ED-006` | **Authorization & Perimeter Security:** Missing, invalid, or mismatched device enrollment tokens reject with HTTP 401/403 and produce zero SQLite writes. | §3.5 | `test_server.py`<br>`test_self_service.py` | `src/ai_asset_inventory/server.py`<br>`src/ai_asset_inventory/database.py` | `pytest tests/test_server.py` | COVERED |

---

## 3. Core Invariants

### 3.1 Privacy Invariant (Hard Boundary)
- Any observation payload, serialized OTLP LogRecord, SQLite database field, or CSV report is rejected by the pipeline if it contains strings matching privacy sentinels or arbitrary untyped metadata.
- All asset names and vendors must validate against the known signature catalog (`SIGNATURES` in `detector.py`).
- Runtime relationship fields (`asset.host_app`, `asset.relationship`) are allowed ONLY for `asset.kind == "agent_runtime"`.

### 3.2 Projection Determinism
- `project_asset(device_id, observed_at, asset)` computes:
  - `logical_key`: SHA-256 digest of stable identity tuple `(schema=1, device.id, asset.kind, asset.name, asset.vendor, asset.host_app, edgedisco.simulated)`.
  - `state_hash`: SHA-256 digest of state tuple `(asset.running, asset.present, asset.relationship, asset.version)`.
- If `state_hash` is unchanged, outbox does not enqueue a redundant state transition.

### 3.3 Outbox Delivery Invariants
- The outbox table (`otlp_outbox`) stores payload JSON serialized under schema version 2.
- Exporter processes events in FIFO order by `id`.
- Successful delivery (`HTTP 200/202/204`) marks the record as delivered and commits the transaction.
- Transport failures (connection refused, timeouts, HTTP 5xx) retain the record with incremented retry count and exponential backoff.

### 3.4 Evidence Class Partitioning
- `present=True, running=False`: Asset is installed or configured on disk, but not currently executing in process table.
- `present=True, running=True`: Asset is installed and currently executing in process table.
- `present=False, running=False`: Asset was previously observed but is no longer detected.
- `present=False, running=True`: **FORBIDDEN** state; rejected by schema validator with `ValueError("invalid asset presence")`.

---

## 4. OTLP Wire Format Specification (Schema v2)

### 4.1 Asset Observation (`edgedisco.asset.observed`)
- **Resource:** `service.name: "edgedisco"`, `service.version: "0.5.0"`
- **Scope:** `ai_asset_inventory.otlp_encoder (v0.5.0)`
- **LogRecord:**
  - `time_unix_nano`: Unix epoch timestamp in nanoseconds
  - `observed_time_unix_nano`: Observation time in nanoseconds
  - `severity_number`: `9` (INFO)
  - `event_name`: `"edgedisco.asset.observed"`
  - `body`: Empty string in native protobuf (synthesized downstream by collector OTTL)
  - `attributes`:
    - `edgedisco.schema.version` (int: `2`)
    - `edgedisco.observation.id` (string: `sha256:<64 hex>`)
    - `device.id` (string: `<32 hex>`)
    - `asset.kind` (string: `"application"` | `"process"` | `"agent_runtime"`)
    - `asset.name` (string)
    - `asset.vendor` (string)
    - `asset.running` (bool)
    - `asset.present` (bool)
    - `edgedisco.simulated` (bool)
    - `asset.host_app` (optional string)
    - `asset.relationship` (optional string: `"spawned_by"` | `"local_process"`)
    - `asset.version` (optional string)

### 4.2 Device Inventory Heartbeat (`edgedisco.device.inventory`)
- **Resource:** `service.name: "edgedisco"`
- **LogRecord:**
  - `event_name`: `"edgedisco.device.inventory"`
  - `body`: `"edgedisco.device.inventory"`
  - `attributes`:
    - `edgedisco.schema.version` (int: `2`)
    - `edgedisco.observation.id` (string: `sha256:<64 hex>`)
    - `device.id` (string: `<32 hex>`)
    - `inventory.asset_count` (int)
    - `inventory.simulated_asset_count` (int)
