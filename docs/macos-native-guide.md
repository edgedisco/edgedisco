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

By default, EdgeDisco operates in local inventory mode. To stream discovered AI asset events to an enterprise OpenTelemetry Collector or SIEM:

### Configure via LaunchDaemon Property List
Edit `/Library/LaunchDaemons/com.edgedisco.daemon.plist` (requires `sudo`):

```xml
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/libexec/edgedisco/edgedisco</string>
    <string>daemon</string>
    <string>--interval</string>
    <string>60</string>
    <string>--db</string>
    <string>/Library/Application Support/EdgeDisco/data/inventory.db</string>
    <string>--ipc-mode</string>
    <string>system</string>
    <string>--ipc-socket</string>
    <string>/var/run/edgedisco.sock</string>
    <string>--ipc-allowed-uid</string>
    <string>0</string>
    <string>--ipc-allowed-gid</string>
    <string>20</string>
    <string>--ipc-owner-uid</string>
    <string>0</string>
    <string>--ipc-group-gid</string>
    <string>20</string>
    <!-- Add OTLP configuration below -->
    <string>--otlp-endpoint</string>
    <string>https://otel-collector.example.com:4318/v1/logs</string>
    <string>--otlp-batch-size</string>
    <string>100</string>
  </array>
```

### Apply and Restart the Daemon
```bash
sudo launchctl bootout system/com.edgedisco.daemon
sudo launchctl bootstrap system /Library/LaunchDaemons/com.edgedisco.daemon.plist
```

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
- The exporter uses an at-least-once transactional outbox pattern with exponential backoff and lease management.
- If the remote collector is temporarily offline, events remain staged in SQLite and are flushed upon reconnection.

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
