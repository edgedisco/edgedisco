# EdgeDisco 🪩

**Discover the AI tools and agents running on an endpoint, without collecting their conversations.**

EdgeDisco is an open-source collector and local dashboard for AI applications, coding agents, model runtimes, and MCP servers. It reports what is installed or observed running, with optional app hooks for agent-session activity. Discovery is based on supported signatures and available endpoint signals; it is not a complete record of every AI tool or action.

## Try it

On **macOS**, supported **systemd Linux**, or **WSL with systemd enabled**, with Python 3.9+ installed:

```bash
curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash
```

To download and inspect the complete installer before running it, use:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh
```

The download-first form is also easier to troubleshoot if a network proxy interrupts streamed
responses. You can review the [self-service installer](https://github.com/edgedisco/edgedisco/blob/main/install.sh) in the repository before running either form.

Open a new terminal, then run:

```bash
edgedisco demo
```

The installer creates a per-user installation under `~/.edgedisco`, starts local services, and opens the dashboard. It does not need `sudo`, Git, or a manually created virtual environment. The demo runs short-lived **simulated test workloads** through the real detector, labels their evidence as simulated, and stops them when it finishes. To open the dashboard later, run `edgedisco dashboard`.

Linux self-service needs an active `systemd --user` session. Use the complete operating-system
guide for installation, verification, upgrades, MCP, OTLP, logs, and removal:

| Task | macOS | Linux |
| --- | --- | --- |
| Install and verify | [macOS steps](docs/deployment-macos.md#1-install-edgedisco) | [Linux steps](docs/deployment-linux.md#1-install-edgedisco) |
| Upgrade or repair | [macOS steps](docs/deployment-macos.md#3-upgrade-or-repair-edgedisco) | [Linux steps](docs/deployment-linux.md#3-upgrade-or-repair-edgedisco) |
| Set up MCP | [macOS steps](docs/deployment-macos.md#4-set-up-the-optional-mcp-server) | [Linux steps](docs/deployment-linux.md#4-set-up-the-optional-mcp-server) |
| Set up OTLP | [macOS steps](docs/deployment-macos.md#5-set-up-optional-otlp-export) | [Linux steps](docs/deployment-linux.md#5-set-up-optional-otlp-export) |

Run `bash install.sh --help` for installer options.

## What it sees

- Supported desktop AI apps, coding tools, local model runtimes, agent frameworks, and bounded editor-extension inventories.
- Supported agent CLIs, including Claude Code, Codex, Gemini CLI, Google Antigravity CLI, Hermes Agent, OpenClaw, Kimi Code, Kilo Code, Mistral Vibe, and others in the [detection catalog](https://github.com/edgedisco/edgedisco/blob/main/docs/detection-catalog.md).
- Current-user processes and parent relationships, plus supported MCP server configurations.
- Sanitized session, subagent, tool, and MCP activity from optional Cursor, Claude Code, and GitHub Copilot hooks.

An installed tool, a configured MCP server, and a running process are different observations. EdgeDisco labels the evidence accordingly. A process scan can miss short-lived activity; hooks provide more detail where supported.

## Privacy and control

EdgeDisco collects inventory and lifecycle **metadata**. Its collector and hooks exclude prompts, responses, source code, documents, tool inputs and outputs, credentials, environment values, raw command lines, and MCP arguments or remote URLs. Supported path and command details are reduced to local fingerprints. Readable binaries found in approved install locations may also receive a SHA-256 content fingerprint; binary contents are not uploaded.

The self-service server binds to localhost. Device uploads use individual credentials, while dashboard access uses a separate administrator credential. The optional MCP interface exposes audited, read-only inventory tools and also binds to localhost. See [MCP inventory access and synchronization](docs/inventory-sync-mcp.md) for setup, tool contracts, pagination, freshness, and reverse-proxy requirements. Review the [architecture and collected fields](docs/architecture.md), [fingerprinting details](https://github.com/edgedisco/edgedisco/blob/main/docs/fingerprinting.md), and [production hardening checklist](docs/production-hardening.md) before a broader deployment.

## Project status

EdgeDisco 0.5.0 is an evaluation and controlled-pilot MVP. The dashboard, CSV evidence exports,
and optional MCP inventory feed work today. Optional OTLP Logs delivery includes a privacy-filtered
projection, durable outbox, and separately supervised exporter process. MCP and OTLP are disabled
unless started or enabled by the operator. Their protocol, privacy, and advanced configuration
references are [MCP inventory access](docs/inventory-sync-mcp.md) and
[OpenTelemetry integration](docs/otel-integration.md).

## Roadmap

Container and VM discovery is planned but is not part of the current collector. The [container and VM discovery roadmap](docs/container-vm-discovery-roadmap.md) separates baseline local-container evidence, presence-only VM inventory, opt-in guest probes, and native guest collectors.

## Develop

Developer setup uses a source checkout; it is separate from the self-service install above.

```bash
git clone https://github.com/edgedisco/edgedisco.git
cd edgedisco
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
make test
```

The command above installs the core package only. To work on the optional integrations, install
their extras from the repository root:

```bash
python -m pip install -e '.[mcp,otlp]'
```

The `mcp` extra requires Python 3.10+. If you use the self-service installer, it creates and uses
its own per-user virtual environment and installs the optional integrations supported by that
Python version; you do not need to activate that environment, and the `edgedisco` command works
from any directory. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. The core supports Python 3.9+.

To test the managed installation flow with uncommitted local changes, run `bash ./install.sh` from the checkout. The installer prints the checkout path, installs that source directly, replaces a same-version managed package, and performs an immediate inventory upload. An installer executed through stdin has no checkout path and downloads the configured source archive instead.

EdgeDisco was created by Neeraj Sabharwal and is licensed under [AGPLv3](LICENSE). [Website](https://edgedisco.com) · [Source](https://github.com/edgedisco/edgedisco)
