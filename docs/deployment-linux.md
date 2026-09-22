# Install and operate EdgeDisco on Linux

This is the complete guide for a managed, per-user EdgeDisco installation on systemd Linux.
Follow it in order for a new installation, or jump to the numbered section for upgrades, MCP, or
OpenTelemetry. For macOS, use the [macOS guide](deployment-macos.md).

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

- A systemd distribution such as Ubuntu, Debian, Fedora, RHEL, Rocky Linux, AlmaLinux, openSUSE,
  or Arch Linux
- A logged-in, non-root user with an active `systemd --user` manager and user D-Bus session
- Python 3.9 or newer with the `venv` module; use Python 3.10 or newer for the optional MCP server
- An internet connection for installation or upgrade

Check the required user service manager before installation:

```bash
systemctl --user show-environment >/dev/null
python3 --version
```

Do not run the installer with `sudo`. Containers, non-systemd systems, WSL without systemd,
headless accounts without a user manager, and SSH sessions without a user D-Bus session are not
supported by the self-service installer.

## 1. Install EdgeDisco

Download the complete installer, inspect it, and run it as the target desktop user:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh
```

The streamed form is also supported:

```bash
curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash
```

The installer asks before making changes. It then:

1. Creates `~/.edgedisco/venv` and installs EdgeDisco with supported MCP and OTLP dependencies.
2. Generates separate administrator and enrollment credentials.
3. Starts the local inventory server on `127.0.0.1:8080`.
4. Enrolls this Linux host and sends the first sanitized inventory report.
5. Installs metadata-only adapters for detected Cursor, Claude Code, and GitHub Copilot apps.
6. Creates `systemd --user` services for the server and collector.
7. Adds the `edgedisco` launcher to `~/.local/bin` and updates the login-shell path.
8. Opens the authenticated dashboard when a browser is available.

Open a new shell after installation so the updated path is active. No virtual environment
activation is needed.

Useful installer options are:

```bash
bash install.sh --help
bash install.sh --no-open
bash install.sh --port 8090
bash install.sh --all-adapters
bash install.sh --yes --no-open
```

Use `EDGEDISCO_HOME` to select a different installer-managed root and `PYTHON_BIN` to select the
Python interpreter. Preserve that `EDGEDISCO_HOME` value for upgrades; pass the same path through
`--root` to later `edgedisco setup`, `status`, `dashboard`, and `uninstall` commands.

## 2. Verify the installation

In a new shell, verify the server, enrollment, and user services:

```bash
edgedisco status
curl http://127.0.0.1:8080/healthz
systemctl --user status com.edgedisco.server.service --no-pager
systemctl --user status com.edgedisco.agent.service --no-pager
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

Download a fresh installer and run it again as the same user with the same managed root:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh --no-open
```

Then verify the upgraded installation:

```bash
edgedisco status
systemctl --user status com.edgedisco.server.service --no-pager
systemctl --user status com.edgedisco.agent.service --no-pager
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
selected Python version supports it. MCP is optional and is not installed as a systemd service;
the following command runs a separate foreground Python process:

```bash
~/.edgedisco/venv/bin/python --version
edgedisco mcp \
  --db ~/.edgedisco/data/inventory.db \
  --host 127.0.0.1 \
  --port 8081
```

Leave that shell open while an MCP client uses:

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

OTLP export is fully disabled by default. Enabling it creates a third `systemd --user` service
running a separate Python process. EdgeDisco supports OTLP Logs over HTTP/protobuf; the endpoint
must be an authenticated HTTPS URL unless it is a literal loopback address.

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

3. Apply the settings and create the exporter service:

   ```bash
   edgedisco setup --no-open
   ```

4. Verify configuration, queue state, and the exporter process:

   ```bash
   edgedisco otlp-status --db ~/.edgedisco/data/inventory.db --json
   systemctl --user status com.edgedisco.otlp-export.service --no-pager
   journalctl --user -u com.edgedisco.otlp-export.service -n 50 --no-pager
   ```

Setup performs a fresh scan after the outbox is enabled. Older inventory is not automatically
backfilled. For the local `otel-stack`, port `4318` is OTLP ingestion, port `3001` is Grafana, and
the endpoint above is correct when the stack runs on the same Linux host.

In an OrbStack or other Linux VM, `127.0.0.1` refers to the VM, not the macOS host. Run the
collector inside the VM, forward it to a VM-loopback listener, or expose the host collector through
an authenticated HTTPS endpoint. Do not substitute a host gateway name in an `http://` URL:
EdgeDisco intentionally permits unencrypted OTLP only to a literal loopback address.

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

The exporter user service is removed, but new state changes continue entering the bounded outbox.

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

This removes the exporter user service and stops creating new OTLP records. Existing outbox rows
and delivery totals remain in the database. See the [OTLP reference](otel-integration.md) for TLS,
headers, batching, retries, retention, payload fields, and live-stack verification.

## 6. Services, files, and logs

The managed processes are:

| Process | systemd user unit | Enabled by default |
| --- | --- | --- |
| Inventory server | `com.edgedisco.server.service` | Yes |
| Collector | `com.edgedisco.agent.service` | Yes |
| OTLP exporter | `com.edgedisco.otlp-export.service` | Only when OTLP delivery is enabled |
| MCP server | None; foreground command | No |

Inspect or restart the core services with:

```bash
systemctl --user status com.edgedisco.server.service com.edgedisco.agent.service --no-pager
systemctl --user restart com.edgedisco.server.service com.edgedisco.agent.service
journalctl --user -u com.edgedisco.server.service -u com.edgedisco.agent.service -f
```

Important files are:

| Location | Purpose |
| --- | --- |
| `~/.edgedisco/server.env` | Server credentials and optional OTLP settings |
| `~/.edgedisco/agent.json` | Endpoint enrollment and collector configuration |
| `~/.edgedisco/data/inventory.db` | Local inventory, sessions, and OTLP outbox |
| `~/.config/systemd/user/com.edgedisco.server.service` | Inventory server user unit |
| `~/.config/systemd/user/com.edgedisco.agent.service` | Collector user unit |
| `~/.config/systemd/user/com.edgedisco.otlp-export.service` | Optional exporter user unit |

Some distributions stop user services after logout unless lingering is enabled. EdgeDisco does
not enable lingering because it is an administrator policy decision. Without lingering, services
start with the user's session and stop when that session ends.

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

If `systemctl --user show-environment` fails, use a graphical or login user session with a working
user manager and D-Bus session. Do not work around it with `sudo`; a root service would inventory
the wrong user.

If port 8080 is already occupied by a server using different credentials, rerun the installer with
another port:

```bash
bash install.sh --port 8090
```

If `edgedisco` is not found after installation, open a new shell or run the managed launcher
directly once:

```bash
~/.local/bin/edgedisco status
```

If agent sessions do not appear, restart the supported app after adapter installation, start a new
agent task, run `edgedisco agent send --config ~/.edgedisco/agent.json`, and inspect the collector
journal. Application inventory alone does not prove that an agent session ran.

The self-service mode is intended for local evaluation and controlled pilots. Before a broader
rollout, complete the [production hardening checklist](production-hardening.md).
