# Native macOS EdgeDisco Guide (Rust + Swift)

This guide covers installation, operations, telemetry configuration, and troubleshooting for the native macOS version of EdgeDisco.

EdgeDisco on macOS is completely native:
- **Rust Core Daemon:** High-performance, low-overhead system service using native Darwin kernel APIs (`libproc`, `sysctl`). No Python runtime or virtual environments required.
- **Swift Menu Bar App:** Native SwiftUI popover (`/Applications/EdgeDisco.app`) communicating with the daemon over a secure Unix domain socket (`/var/run/edgedisco.sock`).
- **No Local Web Server:** Replaces legacy loopback HTTP servers (`127.0.0.1:8080`) with root/staff privilege-separated IPC.

---

## 1. Installation

### From GitHub Release Package (`.pkg`)
Download the latest `EdgeDisco-<version>-unsigned.pkg` from [GitHub Releases](https://github.com/edgedisco/edgedisco/releases).

Install via GUI (double-click the `.pkg`) or terminal:
```bash
sudo installer -pkg EdgeDisco-0.5.2-alpha-unsigned.pkg -target /
```

The installer installs:
- Binary: `/usr/local/libexec/edgedisco/edgedisco`
- Menu Bar App: `/Applications/EdgeDisco.app`
- LaunchDaemon: `/Library/LaunchDaemons/com.edgedisco.daemon.plist`
- LaunchAgent: `/Library/LaunchAgents/com.edgedisco.agent.plist`
- Application Support: `/Library/Application Support/EdgeDisco/` (logs, data, config)
- Socket: `/var/run/edgedisco.sock`

---

## 2. How Discovery and Scanning Work

1. **Scheduled Background Discovery:**
   - The LaunchDaemon (`com.edgedisco.daemon`) automatically runs an observation pass **every 60 seconds**.
   - It enumerates processes using native `libproc` and `sysctl KERN_PROCARGS2` calls to match running processes against the AI detection catalog.
   - It checks local container sockets (Docker and Colima).
   - Results are sanitized (file paths and credentials redacted with SHA-256 digests) and stored in `/Library/Application Support/EdgeDisco/data/inventory.db`.

2. **On-Demand Scans:**
   - Click the EdgeDisco icon in the macOS menu bar and choose **"Scan Now"**.
   - The Swift UI sends an IPC trigger over `/var/run/edgedisco.sock`. The daemon immediately executes an observation pass and refreshes the asset and device counts.

3. **Viewing Detections:**
   - Click **"View Detections"** in the menu bar popover to see classified AI runtimes, CLI agents, local models, and developer tools detected on your machine.

---

## 3. Configuring OpenTelemetry (OTLP) Export

The menu-bar app defaults to **My Session**, backed by the login user's persistent
daemon. Choose **This Mac** for the privileged system daemon. **Open Inventory**
opens a reusable, resizable window with search, state filters, versions, types, and
last-seen timestamps. Scan Now and Refresh operate on the selected scope. The
popover labels its counter **Last scan findings**; the window counts evidence
records, which can include both installed and process evidence for one product.
Previously loaded records are explicitly marked potentially stale if refresh fails.

Existing installations need the updated package to replace the scheduled user
collector with the persistent daemon. Before that upgrade, My Session can report
unavailable even while This Mac works. Closing the inventory window keeps the
menu-bar app running. Settings and live configuration updates remain planned;
the instructions below configure the system daemon.

By default, EdgeDisco operates in local inventory mode. To stream discovered AI asset events to an enterprise OpenTelemetry Collector or SIEM:

### Persistent configuration
Create `/Library/Application Support/EdgeDisco/config/daemon.json` as
`root:wheel`, mode `0600`. Package upgrades preserve this file.

```json
{
  "schema_version": 1,
  "interval_seconds": 60,
  "otlp_endpoint": "https://otel-collector.example.com:4318/v1/logs",
  "otlp_batch_size": 100,
  "otlp_headers": "Authorization=Bearer%20TOKEN",
  "otlp_compression": "gzip",
  "otlp_timeout_ms": 10000,
  "otlp_ca_certificate": "/Library/Application Support/EdgeDisco/config/collector-ca.pem",
  "otlp_client_certificate": "/Library/Application Support/EdgeDisco/config/client.pem",
  "otlp_client_key": "/Library/Application Support/EdgeDisco/config/client-key.pem"
}
```

Fields other than `schema_version` are optional. A missing file uses CLI defaults.
The OTLP header string uses comma-separated `key=value` entries with percent-encoded values.
Header names are case-insensitive; duplicate, transport-owned, and malformed headers are rejected.
Keep the configuration file at mode `0600` when it contains headers. The CA and client certificate
files must be regular files without group or world write access; the client key must be mode `0600`.
Configure the client certificate and key together. Omit unused TLS fields and compression settings.
Values in the file override the corresponding CLI arguments. Omit `otlp_endpoint`
to use the CLI endpoint (the packaged service has none, so export is disabled).
Unknown fields, unsupported versions, malformed JSON, zero intervals/batch sizes,
and invalid endpoints cause startup to fail before opening the database. The daemon
never rewrites the file. Keep IPC identity and database paths in the managed plist;
they are installation policy rather than user tuning.

When upgrading an older installation, the installer imports `--interval`,
`--otlp-endpoint`, and `--otlp-batch-size` from the old daemon plist if no JSON
configuration exists. It retains that plist in a private `config/upgrade.*`
directory. Other custom plist arguments are not imported; review them before an
upgrade. Future config schemas must have explicit compatibility handling before
release; version 1 is the only schema currently supported.

### Apply and Restart the Daemon
```bash
sudo launchctl bootout system/com.edgedisco.daemon
sudo /usr/local/libexec/edgedisco/edgedisco daemon --prepare \
  --config '/Library/Application Support/EdgeDisco/config/daemon.json' \
  --db '/Library/Application Support/EdgeDisco/data/inventory.db'
sudo launchctl bootstrap system /Library/LaunchDaemons/com.edgedisco.daemon.plist
```

Only restart after preparation succeeds. Preparation validates settings and opens
or migrates the database; it does not scan, send telemetry, or bind an IPC socket.
See [upgrade and recovery](macos-enterprise-package.md#upgrade-lifecycle) for backup
locations and downgrade limitations.

### Testing OTLP Export from CLI
You can also run a one-shot collection and export directly from the terminal:
```bash
/usr/local/libexec/edgedisco/edgedisco daemon \
  --once \
  --db "/Library/Application Support/EdgeDisco/data/inventory.db" \
  --otlp-endpoint "https://otel-collector.example.com:4318/v1/logs"
```

### OTLP Outbox & Delivery Guarantees
- Discovered asset events are recorded to the SQLite `otlp_outbox` table.
- Requests use OTLP Logs over HTTP with binary protobuf and
  `Content-Type: application/x-protobuf`.
- Native encoder regression tests decode the wire payload and compare both asset observations and
  device heartbeats with the Python golden protobuf fixtures under `tests/fixtures/golden_otlp`.
- The daemon polls the outbox every second independently of process scans. The exporter uses
  at-least-once delivery with exponential backoff, honors a collector's bounded `Retry-After`,
  and fences completions by lease ownership.
- The active queue is limited to 5,000 records and 16 MiB. Older queued records may be dropped
  under pressure; active leases are preserved. Queued records expire after seven days, while
  delivered and failed records are retained for one and seven days respectively.
- If the remote collector is temporarily offline, queued events are retried upon reconnection,
  subject to the queue limits and retention period above.

---

## 4. Logs and Status Verification

Check daemon output and error logs:
```bash
# Standard output
tail -f "/Library/Application Support/EdgeDisco/logs/daemon.log"

# Error / export logs
tail -f "/Library/Application Support/EdgeDisco/logs/daemon.error.log"
```

Verify the system daemon service in `launchd`:
```bash
sudo launchctl print system/com.edgedisco.daemon
```

Verify the IPC socket permissions:
```bash
ls -l /var/run/edgedisco.sock
# Expected: srwxrwxr-x 1 root staff ... /var/run/edgedisco.sock
```

---

## 5. Uninstallation

EdgeDisco includes an enterprise uninstaller script:

```bash
# Standard uninstall (removes binaries, plists, and services; preserves inventory database):
sudo /usr/local/libexec/edgedisco/uninstall.sh

# Complete purge (removes all files, logs, and SQLite database):
sudo /usr/local/libexec/edgedisco/uninstall.sh --purge
```
