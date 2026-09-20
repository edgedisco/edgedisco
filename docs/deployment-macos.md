# Deploy on macOS

For systemd-based Linux, use the separate [Linux deployment guide](deployment-linux.md).

This guide runs the EdgeDisco server and collector on one Mac for evaluation. A production rollout should host the server centrally behind HTTPS and install only the collector on managed endpoints.

## Self-service installation

Requirements:

- macOS 12 or newer (supported; the installer does not enforce the OS version)
- Python 3.9 or newer
- An internet connection for the initial install

Download, inspect, and run the installer:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh
```

Inspect built-in help without changing the system:

```bash
bash install.sh --help
```

| Option | Description |
| --- | --- |
| `-h`, `--help` | Show all options, environment overrides, and examples, then exit |
| `--yes` | Skip the confirmation prompt after the script has been reviewed |
| `--port PORT` | Set the localhost server port; the default is `8080` |
| `--all-adapters` | Install every supported app adapter instead of only detected adapters |
| `--no-open` | Suppress browser launch; use `edgedisco dashboard` afterward for an authenticated session |

The installer also accepts `EDGEDISCO_HOME` for a custom managed root, `PYTHON_BIN` for the Python 3.9+ interpreter, and `EDGEDISCO_ARCHIVE_URL` for the archive fetched by remote/stdin installation. A local `./install.sh` run from a checkout installs that checkout and does not use the archive URL. Preserve the same `EDGEDISCO_HOME` value across upgrades.

The installer does not use `sudo` and does not request Full Disk Access, Accessibility, Automation, Screen Recording, or Input Monitoring. It:

1. Creates `~/.edgedisco/venv` and installs EdgeDisco there.
2. Generates distinct administrator and enrollment credentials.
3. Starts a local server on `127.0.0.1:8080`.
4. Enrolls the Mac and sends its first sanitized inventory report.
5. Installs metadata-only adapters for detected Cursor, Claude Code, and GitHub Copilot installations.
6. Creates per-user LaunchAgents for the server and collector.
7. Installs a stable `edgedisco` command for normal Terminal sessions.
8. Opens the dashboard. The administrator token stays in the protected local configuration file.

The services start whenever that user logs in. Credentials are stored with user-only permissions in `~/.edgedisco/server.env`; they are not embedded in LaunchAgent files.

The collector stays inside `/Applications`, `~/Applications`, explicitly allowlisted executable directories, supported MCP configuration files, and current-user process metadata. It does not search Desktop, Documents, Downloads, iCloud Drive, network volumes, removable media, or other users' processes. Unreadable files are skipped without retrying with elevated privileges. macOS may show its normal Background Items notification when the per-user LaunchAgents are installed.

Use another local port if 8080 is already assigned:

```bash
bash install.sh --port 8090
```

Install every supported adapter, including apps that are not currently detected:

```bash
bash install.sh --all-adapters
```

For unattended test machines, review the script first and then pass `--yes --no-open`.

## Verify the installation

Open a new Terminal window after install, then:

```bash
edgedisco status
curl http://127.0.0.1:8080/healthz
```

Run the local discovery demo (simulated workloads, real detector; no API keys):

```bash
edgedisco demo
```

The self-service installer installs a stable `edgedisco` command for normal shells. You do not need to activate a virtual environment or type the internal install path.

The status output should show `Server: healthy` and `Endpoint: enrolled`. Open `http://127.0.0.1:8080`. If manual sign-in is needed, copy the token without printing it:

```bash
grep '^export AAI_ADMIN_TOKEN=' ~/.edgedisco/server.env | cut -d= -f2- | tr -d '\n' | pbcopy
```

Paste with **Command+V** and do not place the administrator token in screenshots, logs, issues, or chat.

Start an agent task in Cursor, Claude Code, or GitHub Copilot. Then wait for the collector cycle or send immediately:

```bash
edgedisco agent send \
  --config ~/.edgedisco/agent.json
```

Refresh the dashboard. The active agent or recent session should appear when the application emits a supported lifecycle hook. Application inventory alone does not prove an agent ran; session evidence comes from the adapters.

## Logs and files

| Location | Purpose |
| --- | --- |
| `~/.edgedisco/server.env` | Local server credentials |
| `~/.edgedisco/agent.json` | Enrolled endpoint configuration |
| `~/.edgedisco/data/inventory.db` | Local compliance evidence |
| `~/.edgedisco/logs/server.log` | Server standard output |
| `~/.edgedisco/logs/server.err.log` | Server errors |
| `~/.edgedisco/logs/agent.log` | Collector standard output |
| `~/.edgedisco/logs/agent.err.log` | Collector errors |

Follow the logs:

```bash
tail -f ~/.edgedisco/logs/server.err.log
tail -f ~/.edgedisco/logs/agent.err.log
```

## Upgrade or repair

Download the current installer and run it again. Existing credentials, enrollment, and evidence are preserved.

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
bash install.sh
```

Reinstall validates `agent.json` before replacing the package. Invalid JSON, invalid settings, or a configuration/database version newer than this release stops the upgrade. Repair the existing file or use a compatible release; reinstall does not silently discard it.

Unversioned and version-1 configuration is migrated to `config_version: 2`, preserving custom settings and adding missing incremental-scanning defaults. The local server URL follows the configured port. Missing or rejected device credentials are re-enrolled with the local enrollment credential; timeouts and server errors do not trigger re-enrollment. Old EdgeDisco hook commands are replaced when paths change, while unrelated hooks remain intact.

The installer prints a protected snapshot directory under `~/.edgedisco/backups/`. Each snapshot includes the previous managed environment, configuration, database, hook files, launch definitions, and shell profiles. Services are stopped before taking the snapshot, not just before package replacement. If installation fails, the installer restores the snapshot and reconciles evidence accepted during setup verification into the restored database before restarting prior services. This includes runtime events already acknowledged and removed from the spool, and supported OTLP outbox state. Fields absent from the old schema remain available in the full failed-attempt database under `failed-state/`. Runtime spool files are never rolled back. A failed stop prevents file replacement.

Snapshots are retained and include credentials and evidence; keep them private. They consume space roughly proportional to the managed environment and database. If automatic recovery cannot finish, the installer reports the snapshot path. From a compatible source checkout, retry recovery with:

```bash
PYTHONPATH=src python3 -m ai_asset_inventory.upgrade restore --backup /absolute/path/to/.edgedisco/backups/TIMESTAMP
```

Database migrations and `PRAGMA user_version` commit in one transaction. This release migrates existing databases to version 2 for binary fingerprint evidence while preserving prior rows. Future schema changes must supply explicit migration steps. These backup/rollback guarantees apply to `install.sh`; running `edgedisco setup` directly does not snapshot the package.

## Final installed-system test

Do not switch an existing quick-installer installation to an editable developer setup just to test an upgrade. Run isolated regressions (`make check`) first; installer fixtures use real package installation and HTTP services but simulate `launchctl`, so they do not certify real LaunchAgent operation.

From the source checkout containing the changes to test:

```bash
bash ./install.sh --yes --no-open
edgedisco status
edgedisco dashboard
```

The local installer installs this checkout into the managed venv and upgrades the existing configuration. The downloaded installer instead tests published `main`; it cannot test unpushed changes. Honor a custom `EDGEDISCO_HOME` if the existing installation uses one. Do not uninstall, purge, or replace the current configuration for this test.

`edgedisco dashboard` creates a fresh one-time browser bootstrap, so developers do not need to display or copy the administrator token. The plain dashboard URL still requires an existing session or manual sign-in. Run `edgedisco demo` separately when synthetic lab evidence is wanted.

Verify preserved device identity/custom settings, config version 2, healthy server and agent services, authenticated dashboard access, and fresh inventory. Start and stop an actual supported agent and refresh the dashboard after a polling interval. Finally start a fresh session in a hooked application and confirm new runtime events and session state. Synthetic demo or SDK events test transport, not native application hook invocation. No Full Disk Access, Accessibility, or Screen Recording permission should be required; stop and investigate unexpected prompts rather than granting them.

## Uninstall

Stop and remove the background services while keeping the local data:

```bash
edgedisco uninstall
```

Uninstall also removes only the EdgeDisco-managed Cursor, Claude Code, and GitHub Copilot hooks. Unrelated application hooks are preserved.

Delete the local credentials, logs, configuration, and evidence as well:

```bash
edgedisco uninstall --purge
```

The purge command asks for confirmation. The noninteractive equivalent is `--purge --yes`.

## Troubleshooting

If setup reports that port 8080 uses different credentials, either stop the old service or choose another port:

```bash
bash install.sh --port 8090
```

If no agent sessions appear:

1. Confirm the app is listed under `Detected adapters` during setup.
2. Restart the app after adapter installation.
3. Run a new agent task, not only a normal chat.
4. Send events immediately with `edgedisco agent send`.
5. Check `~/.edgedisco/logs/agent.err.log`.

## Managed enterprise rollout

The self-service mode is intended for a local evaluation. For managed endpoints, host the API behind HTTPS, provision endpoint configuration through MDM, and run one per-user collector in each target user's graphical session. Do not convert it to a root LaunchDaemon: the scanner deliberately observes only its own user. Define retention and access controls and complete the [production hardening checklist](production-hardening.md). A future signed app distribution should use Apple's supported background-service APIs.
