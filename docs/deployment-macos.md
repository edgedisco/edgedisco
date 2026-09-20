# Deploy on macOS

This guide runs the EdgeDisco server and collector on one Mac for evaluation. A production rollout should host the server centrally behind HTTPS and install only the collector on managed endpoints.

## Self-service installation

Requirements:

- macOS 12 or newer
- Python 3.9 or newer
- An internet connection for the initial install

Download, inspect, and run the installer:

```bash
curl -fsSLO https://raw.githubusercontent.com/nsabharwal/edgedisco/main/install.sh
less install.sh
bash install.sh
```

The installer does not use `sudo`. It:

1. Creates `~/.edgedisco/venv` and installs EdgeDisco there.
2. Generates distinct administrator and enrollment credentials.
3. Starts a local server on `127.0.0.1:8080`.
4. Enrolls the Mac and sends its first sanitized inventory report.
5. Installs metadata-only adapters for detected Cursor, Claude Code, and GitHub Copilot installations.
6. Creates per-user LaunchAgents for the server and collector.
7. Installs a stable `edgedisco` command for normal Terminal sessions.
8. Opens the dashboard. The administrator token stays in the protected local configuration file.

The services start whenever that user logs in. Credentials are stored with user-only permissions in `~/.edgedisco/server.env`; they are not embedded in LaunchAgent files.

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

The status output should show `Server: healthy` and `Endpoint: enrolled`. Open `http://127.0.0.1:8080` and sign in with:

```bash
source ~/.edgedisco/server.env
echo "$AAI_ADMIN_TOKEN"
```

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
curl -fsSLO https://raw.githubusercontent.com/nsabharwal/edgedisco/main/install.sh
bash install.sh
```

## Uninstall

Stop and remove the background services while keeping the local data:

```bash
edgedisco uninstall
```

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

The self-service mode is intended for a local evaluation. For managed endpoints, host the API behind HTTPS, provision only endpoint configuration through MDM, use an appropriate service identity, define retention and access controls, and complete the [production hardening checklist](production-hardening.md). The example `deploy/com.trust3.ai-inventory.plist` can be adapted for that model.
