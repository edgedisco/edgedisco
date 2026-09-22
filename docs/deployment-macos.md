# Install and operate EdgeDisco on macOS

This is the complete guide for a managed, per-user EdgeDisco installation on macOS. Follow it in
order for a new installation, or jump to the numbered section for upgrades, MCP, or OpenTelemetry.
For Linux, use the [Linux guide](deployment-linux.md).

The managed installation uses `~/.edgedisco`, creates its own Python virtual environment, and
installs a stable `edgedisco` command. Do not activate the managed virtual environment.

| Goal | Section |
| --- | --- |
| New installation | [1. Install EdgeDisco](#1-install-edgedisco) |
| Confirm it works | [2. Verify the installation](#2-verify-the-installation) |
| Upgrade or repair | [3. Upgrade or repair EdgeDisco](#3-upgrade-or-repair-edgedisco) |
| Start MCP | [4. Set up the optional MCP server](#4-set-up-the-optional-mcp-server) |
| Enable or disable OTLP | [5. Set up optional OTLP export](#5-set-up-optional-otlp-export) |
| Remove EdgeDisco | [7. Uninstall](#7-uninstall) |

## Requirements

- macOS 12 or newer
- Python 3.9 or newer; use Python 3.10 or newer if you plan to run the optional MCP server
- An internet connection for installation or upgrade
- A normal user account; do not use `sudo`

The installer does not request Full Disk Access, Accessibility, Automation, Screen Recording, or
Input Monitoring. macOS may show its normal Background Items notification when the per-user
LaunchAgents are installed.

## 1. Install EdgeDisco

Install with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash
```

The installer asks for confirmation through the terminal before making changes. To download and
inspect the complete installer first, use:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh
```

It then:

1. Creates `~/.edgedisco/venv` and installs EdgeDisco with supported MCP and OTLP dependencies.
2. Generates separate administrator and enrollment credentials.
3. Starts the local inventory server on `127.0.0.1:8080`.
4. Enrolls this Mac and sends the first sanitized inventory report.
5. Installs metadata-only adapters for detected Cursor, Claude Code, and GitHub Copilot apps.
6. Creates per-user LaunchAgents for the server and collector.
7. Adds the `edgedisco` launcher to `~/.local/bin` and updates the login-shell path.
8. Opens the authenticated dashboard.

Open a new Terminal window after installation so the updated path is active. No virtual
environment activation is needed.

Useful installer options are:

```bash
curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash -s -- --no-open
bash install.sh --help
bash install.sh --no-open
bash install.sh --port 8090
bash install.sh --all-adapters
bash install.sh --yes --no-open
```

For the streamed command, place installer options after `bash -s --`. The `bash install.sh`
examples apply when using the downloaded copy.

Use `EDGEDISCO_HOME` to select a different installer-managed root and `PYTHON_BIN` to select the
Python interpreter. Preserve that `EDGEDISCO_HOME` value for upgrades; pass the same path through
`--root` to later `edgedisco setup`, `status`, `dashboard`, and `uninstall` commands.

## 2. Verify the installation

In a new Terminal, verify the server, enrollment, and LaunchAgents:

```bash
edgedisco status
curl http://127.0.0.1:8080/healthz
launchctl print "gui/$(id -u)/com.edgedisco.server"
launchctl print "gui/$(id -u)/com.edgedisco.agent"
```

`edgedisco status` should report a healthy server and an enrolled endpoint. Open an authenticated
dashboard session and optionally run the simulated discovery demo:

```bash
edgedisco dashboard
edgedisco demo
```

The demo creates clearly labeled simulated evidence and does not require an API key. To force an
immediate real inventory upload after installing or starting a supported tool, run:

```bash
edgedisco agent send --config ~/.edgedisco/agent.json
```

## 3. Upgrade or repair EdgeDisco

Run the one-line installer again with the same user and managed root:

```bash
curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash
```

For an inspect-before-running upgrade:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh --no-open
```

Then verify the upgraded installation:

```bash
edgedisco status
edgedisco dashboard
```

An upgrade preserves credentials, enrollment, custom configuration, evidence, and unrelated app
hooks. It validates the existing configuration and database, stops the managed services, and
creates a protected snapshot under `~/.edgedisco/backups/` before replacing files. If setup or
verification fails, the installer restores the previous installation and reports the snapshot and
failed-attempt location. These backups contain credentials and evidence; keep them private.

Running `bash ./install.sh --yes --no-open` from a source checkout installs that checkout and is
the supported way to test uncommitted installer changes. A downloaded installer always installs
the published `main` branch.

## 4. Set up the optional MCP server

MCP requires Python 3.10 or newer. The managed installer includes the MCP dependency when its
selected Python version supports it. MCP is optional and is not installed as a LaunchAgent; the
following command runs a separate foreground Python process:

```bash
~/.edgedisco/venv/bin/python --version
edgedisco mcp \
  --db ~/.edgedisco/data/inventory.db \
  --host 127.0.0.1 \
  --port 8081
```

Leave that Terminal open while an MCP client uses:

```text
http://127.0.0.1:8081/mcp
```

Stop the MCP server with Control-C. Its default audit log is
`~/.edgedisco/data/mcp-audit.jsonl`. Use `--audit-log /path/to/audit.jsonl` to select another file.
The server accepts loopback bind addresses only and exposes read-only, privacy-filtered tools.

If the command reports a missing MCP dependency, the managed environment was created with Python
3.9 or an older installation without the extra. If `~/.edgedisco/venv/bin/python --version`
reports 3.10 or newer, follow the upgrade steps once to install the extra. Otherwise, use the
[manual source-checkout instructions](inventory-sync-mcp.md#manual-source-checkout) with Python
3.10 or newer. Tool schemas, pagination, audit contents, and reverse-proxy requirements are
documented in the [MCP reference](inventory-sync-mcp.md).

## 5. Set up optional OTLP export

OTLP export is fully disabled by default. Enabling it creates a third LaunchAgent running a
separate Python process. EdgeDisco supports OTLP Logs over HTTP/protobuf; the endpoint must be an
authenticated HTTPS URL unless it is a literal loopback address.

### Enable delivery

1. Open the protected managed configuration:

   ```bash
   nano ~/.edgedisco/server.env
   ```

2. Add or update these lines, keeping all existing credential lines:

   ```dotenv
   export EDGEDISCO_OTLP_OUTBOX_ENABLED=true
   export EDGEDISCO_OTLP_EXPORT_ENABLED=true
   export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
   ```

3. Apply the settings and create the exporter LaunchAgent:

   ```bash
   edgedisco setup --no-open
   ```

4. Verify configuration, queue state, and the exporter process:

   ```bash
   edgedisco otlp-status --db ~/.edgedisco/data/inventory.db --json
   launchctl print "gui/$(id -u)/com.edgedisco.otlp-export"
   tail -n 50 ~/.edgedisco/logs/otlp-export.err.log
   ```

Setup performs a fresh scan after the outbox is enabled. Older inventory is not automatically
backfilled. For the local `otel-stack`, port `4318` is OTLP ingestion, port `3001` is Grafana, and
the endpoint above is correct.

### Pause delivery and keep queuing

Set these values in `~/.edgedisco/server.env`, then apply them:

```dotenv
export EDGEDISCO_OTLP_OUTBOX_ENABLED=true
export EDGEDISCO_OTLP_EXPORT_ENABLED=false
```

```bash
edgedisco setup --no-open
edgedisco otlp-status --db ~/.edgedisco/data/inventory.db --json
```

The exporter LaunchAgent is removed, but new state changes continue entering the bounded outbox.

### Disable OTLP completely

Set both values to `false`, remove unused endpoint or authentication settings, and apply them:

```dotenv
export EDGEDISCO_OTLP_OUTBOX_ENABLED=false
export EDGEDISCO_OTLP_EXPORT_ENABLED=false
```

```bash
edgedisco setup --no-open
edgedisco otlp-status --db ~/.edgedisco/data/inventory.db --json
```

This removes the exporter LaunchAgent and stops creating new OTLP records. Existing outbox rows
and delivery totals remain in the database. See the [OTLP reference](otel-integration.md) for TLS,
headers, batching, retries, retention, payload fields, and live-stack verification.

## 6. Services, files, and logs

The managed processes are:

| Process | LaunchAgent | Enabled by default |
| --- | --- | --- |
| Inventory server | `com.edgedisco.server` | Yes |
| Collector | `com.edgedisco.agent` | Yes |
| OTLP exporter | `com.edgedisco.otlp-export` | Only when OTLP delivery is enabled |
| MCP server | None; foreground command | No |

Manage background services with:

```bash
edgedisco status
edgedisco stop
edgedisco start
edgedisco restart

# Or target individual services:
edgedisco stop agent
edgedisco start agent
edgedisco restart server
```

Important files are:

| Location | Purpose |
| --- | --- |
| `~/.edgedisco/server.env` | Server credentials and optional OTLP settings |
| `~/.edgedisco/agent.json` | Endpoint enrollment and collector configuration |
| `~/.edgedisco/data/inventory.db` | Local inventory, sessions, and OTLP outbox |
| `~/.edgedisco/logs/server.err.log` | Inventory server errors |
| `~/.edgedisco/logs/agent.err.log` | Collector errors |
| `~/.edgedisco/logs/otlp-export.err.log` | OTLP exporter errors when enabled |

Follow logs with:

```bash
tail -f ~/.edgedisco/logs/server.err.log
tail -f ~/.edgedisco/logs/agent.err.log
tail -f ~/.edgedisco/logs/otlp-export.err.log
```

## 7. Uninstall

Remove the managed services, launcher, and EdgeDisco-managed app hooks while retaining data:

```bash
edgedisco uninstall
```

Also delete local credentials, configuration, logs, backups, and evidence:

```bash
edgedisco uninstall --purge
```

The purge command asks for confirmation. Use `edgedisco uninstall --purge --yes` for a
noninteractive purge.

## Troubleshooting

If port 8080 is already occupied by a server using different credentials, rerun the installer with
another port:

```bash
bash install.sh --port 8090
```

If `edgedisco` is not found after installation, open a new Terminal or run the managed launcher
directly once:

```bash
~/.local/bin/edgedisco status
```

If agent sessions do not appear, restart the supported app after adapter installation, start a new
agent task, run `edgedisco agent send --config ~/.edgedisco/agent.json`, and inspect the collector
error log. Application inventory alone does not prove that an agent session ran.

The self-service mode is intended for local evaluation and controlled pilots. Before a broader
rollout, complete the [production hardening checklist](production-hardening.md).
