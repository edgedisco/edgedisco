# EdgeDisco

Privacy-preserving endpoint discovery for local AI applications, agent frameworks, and MCP servers.

EdgeDisco is an open-source project created and owned by [Neeraj Sabharwal](https://www.linkedin.com/in/neerajsabharwal/). It is released under the Apache License 2.0.

EdgeDisco runs a lightweight collector on macOS, Windows, or Linux and sends sanitized inventory and agent lifecycle evidence to a central compliance dashboard. It answers two enterprise questions: **which AI tools are present, and which agents are actually running inside them?**

> Status: pilot-ready MVP. Review the [production hardening checklist](docs/production-hardening.md) before a broad enterprise rollout.

## Why this exists

AI applications are being installed and executed faster than security teams can inventory them. Traditional software inventory can identify installed applications, but often misses local model runtimes, Python or Node agent frameworks, and MCP servers configured inside developer tools.

This project provides a focused inventory layer without collecting employee content.

## Capabilities

- Discovers supported AI desktop applications and local model runtimes
- Detects supported AI and agent processes while they are running
- Links active agent runtimes to the desktop tool that spawned them using local process lineage
- Captures agent sessions, subagents, tool use, MCP use, status, model, and duration through native app hooks
- Includes adapters for Cursor, Claude Code, and GitHub Copilot plus a generic Python SDK
- Inventories MCP server names, transport types, and executable basenames
- Records first seen, last seen, device, OS, vendor, type, and running state
- Provides a centralized dashboard and CSV evidence export
- Uses unique device upload credentials after enrollment
- Works without third-party Python runtime dependencies

Current signatures include ChatGPT, Claude, Cursor, GitHub Copilot, Windsurf, Ollama, LM Studio, Jan, AnythingLLM, Open WebUI, LocalAI, Dify, CrewAI, AutoGen, LangGraph/LangChain, and MCP servers.

For active agent runtimes, the inventory reports the framework, host application, runtime executable, relationship, and aggregated instance count. Native hook adapters add session and tool lifecycle evidence without sending hook payloads, process IDs, prompts, responses, code, tool arguments, or raw commands.

## Privacy boundary

The collector records inventory metadata. It does **not** collect:

- Prompts, responses, documents, clipboard contents, or keystrokes
- Screenshots, browser history, web page contents, or network payloads
- Environment variable values, API keys, authorization headers, or cookies
- MCP arguments, environment values, headers, or remote URLs
- Full executable paths or raw command lines

Paths and sanitized command structures are converted to SHA-256 fingerprints on the endpoint. Unknown processes are ignored by default.

## Architecture

```mermaid
flowchart LR
  H[App hooks and SDK] --> A[Endpoint collector]
  A -->|TLS and device token| B[Inventory API]
  B --> C[(SQLite evidence store)]
  C --> D[Compliance dashboard]
  C --> E[CSV export]
```

The shared enrollment credential is used only to create a device identity. Each enrolled endpoint receives a unique upload token. Endpoint credentials cannot read fleet inventory. A separate administrator token protects the dashboard and export APIs.

See [Architecture](docs/architecture.md) for the data flow and trust boundaries.

## Requirements

- Python 3.9 or newer
- macOS, Windows, or Linux
- Permission to list local processes
- HTTPS ingress for any non-local deployment

## Self-service install on macOS

The recommended installer creates an isolated environment under `~/.edgedisco`, generates and stores the required credentials, enrolls the Mac, installs adapters for detected AI applications, starts the server and collector at login, and opens the dashboard.

Download and inspect the installer, then run it:

```bash
curl -fsSLO https://raw.githubusercontent.com/nsabharwal/edgedisco/main/install.sh
less install.sh
bash install.sh
```

For a disposable evaluation Mac, the same installer can be run directly:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/nsabharwal/edgedisco/main/install.sh)"
```

No `sudo` is required. When setup finishes, the terminal prints the local dashboard address and its administrator token. To check the installation later:

```bash
~/.edgedisco/venv/bin/edgedisco status
```

To display the administrator token again:

```bash
source ~/.edgedisco/server.env
echo "$AAI_ADMIN_TOKEN"
```

Rerun `bash install.sh` to upgrade or repair the installation. Remove the background services while retaining local evidence with:

```bash
~/.edgedisco/venv/bin/edgedisco uninstall
```

Add `--purge` only when you also want to delete credentials, logs, configuration, and collected evidence. See the [macOS self-service guide](docs/deployment-macos.md) for testing and troubleshooting.

## Developer setup from source

Clone the repository and create an isolated environment:

```bash
git clone https://github.com/nsabharwal/edgedisco.git
cd edgedisco
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Generate two different server credentials:

```bash
export AAI_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AAI_ENROLLMENT_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

umask 077
printf 'export AAI_ADMIN_TOKEN=%s\nexport AAI_ENROLLMENT_TOKEN=%s\n' \
  "$AAI_ADMIN_TOKEN" "$AAI_ENROLLMENT_TOKEN" > server.env

echo "Dashboard admin token: $AAI_ADMIN_TOKEN"
```

Start the central server:

```bash
ai-inventory server --host 127.0.0.1 --port 8080 --db data/inventory.db
```

Open `http://127.0.0.1:8080` and sign in with `AAI_ADMIN_TOKEN`.

The server refuses credentials shorter than 24 characters or identical administrator and enrollment credentials.

## Configure an endpoint

In a second terminal, load the same enrollment credential and create the endpoint configuration:

```bash
cd edgedisco
. .venv/bin/activate
source server.env
cat > agent.json <<EOF
{
  "server_url": "http://127.0.0.1:8080",
  "enrollment_token": "$AAI_ENROLLMENT_TOKEN",
  "scan_interval_seconds": 300
}
EOF
chmod 600 agent.json
```

Preview, enroll, and send the first report:

```bash
ai-inventory agent scan --config agent.json
ai-inventory agent enroll --config agent.json
ai-inventory agent send --config agent.json
```

Install runtime adapters for the apps present on the endpoint:

```bash
ai-inventory adapters install --config "$(pwd)/agent.json" \
  --apps cursor claude-code github-copilot
```

Restart or reload the relevant application, run an agent task, then send the captured runtime events:

```bash
ai-inventory agent send --config agent.json
```

For continuous inventory and runtime-event forwarding:

```bash
ai-inventory agent run --config agent.json
```

After enrollment, the shared enrollment credential is removed from the configuration and replaced with a unique `device_id` and `device_token`.

See [Runtime adapters](docs/runtime-adapters.md) for app-specific details and the generic SDK.

## Managed deployment

For self-service testing and managed macOS deployment guidance, see [Deploy on macOS](docs/deployment-macos.md).

Other deployment assets:

- Linux systemd unit: `deploy/ai-inventory-agent.service`
- Windows configuration helper: `deploy/install-agent.ps1`
- macOS LaunchDaemon example: `deploy/com.trust3.ai-inventory.plist`

## Container deployment

```bash
export AAI_ADMIN_TOKEN="replace-with-a-long-random-value"
export AAI_ENROLLMENT_TOKEN="replace-with-a-different-long-random-value"
docker compose up --build -d
```

The included Compose configuration binds the server to localhost. Put it behind an HTTPS reverse proxy or load balancer before endpoints connect over a network.

## API

| Method | Path | Credential | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/enroll` | Enrollment token | Create a device identity |
| `POST` | `/api/v1/reports` | Device token | Upload sanitized inventory |
| `POST` | `/api/v1/runtime-events` | Device token | Upload sanitized agent lifecycle events |
| `GET` | `/api/v1/summary` | Admin token or session | Read fleet inventory |
| `GET` | `/api/v1/export.csv` | Admin token or session | Export compliance evidence |
| `GET` | `/api/v1/agent-sessions.csv` | Admin token or session | Export agent-session evidence |
| `GET` | `/healthz` | None | Health check |

For API access, pass `Authorization: Bearer <token>`.

## Development

Run the automated tests:

```bash
make test
```

Build a wheel:

```bash
make build
```

The GitHub Actions workflow runs the test suite on Python 3.9 through 3.13. See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes.

## Current limitations

- Ordinary browser-tab usage is not detected. That requires an approved browser extension, DNS/SWG telemetry, or browser-management integration.
- Agents that execute entirely inside a SaaS control plane are not visible to endpoint hooks and require that platform's audit or inventory API.
- The central server uses SQLite and one administrator role.
- Automated retention, device-token revocation, and SSO are not implemented.
- Detection is signature-based and should be aligned with the organization's approved and prohibited application catalog.
- A native signed installer is not included; the macOS self-service installer is a reviewable shell script.

## Responsible deployment

Before installing on employee devices:

- Review collected fields with Security, Privacy, Legal, HR, and employee representatives.
- Publish an employee notice describing the purpose, collected metadata, access controls, and retention period.
- Use HTTPS, protect configuration files, rotate enrollment credentials, and restrict dashboard access.
- Complete the [production hardening checklist](docs/production-hardening.md).

## Security

See [SECURITY.md](SECURITY.md) for the security model and private reporting guidance.

## License

Apache License 2.0. See [LICENSE](LICENSE).
