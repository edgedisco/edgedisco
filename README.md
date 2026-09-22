# EdgeDisco 🪩

**Discover the AI tools and agents running on an endpoint, without collecting their conversations.**

EdgeDisco is an open-source collector and local dashboard for AI applications, coding agents, model runtimes, and MCP servers. It distinguishes software that is installed, configured, running, or observed through supported session hooks.

## Install

On macOS 12+, supported systemd Linux, or WSL with systemd enabled, install EdgeDisco with one command:

```bash
curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash
```

Open a new terminal, then check the service, open the dashboard, or generate safe sample activity:

```bash
edgedisco status
edgedisco dashboard
edgedisco demo
```

The installer creates a per-user installation under `~/.edgedisco` and starts the local services. It does not need `sudo`, Git, a particular working directory, or a manually created virtual environment. Python 3.9+ is required; the optional MCP server requires Python 3.10+.

Use the complete platform guide for prerequisites, an inspect-before-running installation, verification, upgrades, optional integrations, logs, and removal:

- [macOS installation and operation](docs/deployment-macos.md)
- [Linux and WSL installation and operation](docs/deployment-linux.md)

The [installer source](install.sh) is available for review. Linux self-service installation requires an active `systemd --user` session.

## What EdgeDisco reports

- Supported desktop AI apps, coding tools, local model runtimes, agent frameworks, and bounded editor-extension inventories.
- Current-user processes and parent relationships.
- Supported MCP server configurations.
- Sanitized session, subagent, tool, and MCP activity from optional Cursor, Claude Code, and GitHub Copilot hooks.

An installed tool, a configured MCP server, and a running process are different observations. EdgeDisco labels the evidence accordingly. A process scan can miss short-lived activity; hooks provide more detail where supported. See the [detection catalog](docs/detection-catalog.md) for the current coverage.

## Privacy and control

EdgeDisco collects inventory and lifecycle metadata. Its collector and hooks exclude prompts, responses, source code, documents, tool inputs and outputs, credentials, environment values, raw command lines, MCP arguments, and remote URLs. Supported path and command details are reduced to local fingerprints. Readable binaries in approved install locations may receive a SHA-256 content fingerprint; binary contents are not uploaded.

The self-service server and optional MCP interface bind to localhost. Device uploads use individual credentials, while dashboard access uses a separate administrator credential. Review the [architecture and collected fields](docs/architecture.md), [fingerprinting details](docs/fingerprinting.md), and [production hardening checklist](docs/production-hardening.md) before a broader deployment.

## Optional integrations

### MCP inventory tools

The MCP server provides audited, read-only tools for querying EdgeDisco inventory. It is disabled by default and does not run as a background service unless you configure one yourself. Follow the [macOS MCP steps](docs/deployment-macos.md#4-set-up-the-optional-mcp-server) or [Linux MCP steps](docs/deployment-linux.md#4-set-up-the-optional-mcp-server), then use the [MCP reference](docs/inventory-sync-mcp.md) for tool contracts, pagination, freshness, and advanced configuration.

### OpenTelemetry export

OTLP export is disabled by default. When enabled, EdgeDisco supervises a separate exporter process with a privacy-filtered OTLP Logs projection and durable outbox. Follow the [macOS OTLP steps](docs/deployment-macos.md#5-set-up-optional-otlp-export) or [Linux OTLP steps](docs/deployment-linux.md#5-set-up-optional-otlp-export), then use the [OpenTelemetry reference](docs/otel-integration.md) for configuration and payload details.

## Documentation

The [documentation index](docs/README.md) groups the guides by task. Common references include:

- [Architecture and collected fields](docs/architecture.md)
- [Detection catalog](docs/detection-catalog.md)
- [Runtime adapter configuration](docs/runtime-adapters.md)
- [MCP inventory access](docs/inventory-sync-mcp.md)
- [OpenTelemetry integration](docs/otel-integration.md)
- [Container and VM discovery roadmap](docs/container-vm-discovery-roadmap.md)

## Project status

EdgeDisco 0.5.0 is an evaluation and controlled-pilot MVP. The local dashboard, CSV evidence exports, optional MCP inventory feed, and optional OTLP Logs delivery work today. Container and VM discovery remains planned work.

## Development

Development uses a source checkout and a project virtual environment. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, optional extras, tests, and contribution requirements.

EdgeDisco was created by Neeraj Sabharwal and is licensed under [AGPLv3](LICENSE). [Website](https://edgedisco.com) · [Source](https://github.com/edgedisco/edgedisco)
