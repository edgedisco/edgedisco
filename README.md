# EdgeDisco 🪩

**Discover the AI tools and agents running on an endpoint, without collecting their conversations.**

EdgeDisco is an open-source collector and local dashboard for AI applications, coding agents, model runtimes, and MCP servers. It reports what is installed or observed running, with optional app hooks for agent-session activity. Discovery is based on supported signatures and available endpoint signals; it is not a complete record of every AI tool or action.

## Try it

On **macOS** or supported **systemd Linux**, with Python 3.9+ installed, download and inspect the [self-service installer](https://github.com/edgedisco/edgedisco/blob/main/install.sh):

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh
```

Open a new terminal, then run:

```bash
edgedisco demo
```

The installer creates a per-user installation under `~/.edgedisco`, starts local services, and opens the dashboard. It does not need `sudo`, Git, or a manually created virtual environment. The demo runs short-lived **simulated test workloads** through the real detector, labels their evidence as simulated, and stops them when it finishes. To open the dashboard later, run `edgedisco dashboard`.

Linux self-service needs an active `systemd --user` session. See the [macOS](docs/deployment-macos.md) and [Linux](https://github.com/edgedisco/edgedisco/blob/main/docs/deployment-linux.md) guides for requirements, upgrades, custom ports, and uninstalling. Run `bash install.sh --help` for installer options.

## What it sees

- Supported desktop AI apps, coding tools, local model runtimes, and agent frameworks.
- Supported agent CLIs, including Claude Code, Codex, Gemini CLI, Aider, and others in the [detection catalog](https://github.com/edgedisco/edgedisco/blob/main/docs/detection-catalog.md).
- Current-user processes and parent relationships, plus supported MCP server configurations.
- Sanitized session, subagent, tool, and MCP activity from optional Cursor, Claude Code, and GitHub Copilot hooks.

An installed tool, a configured MCP server, and a running process are different observations. EdgeDisco labels the evidence accordingly. A process scan can miss short-lived activity; hooks provide more detail where supported.

## Privacy and control

EdgeDisco collects inventory and lifecycle **metadata**. Its collector and hooks exclude prompts, responses, source code, documents, tool inputs and outputs, credentials, environment values, raw command lines, and MCP arguments or remote URLs. Supported path and command details are reduced to local fingerprints. Readable binaries found in approved install locations may also receive a SHA-256 content fingerprint; binary contents are not uploaded.

The self-service server binds to localhost. Device uploads use individual credentials, while dashboard access uses a separate administrator credential. The optional MCP interface exposes six audited, read-only inventory tools and also binds to localhost. Review the [architecture and collected fields](docs/architecture.md), [fingerprinting details](https://github.com/edgedisco/edgedisco/blob/main/docs/fingerprinting.md), and [production hardening checklist](docs/production-hardening.md) before a broader deployment.

## Project status

EdgeDisco 0.5.0 is an evaluation and controlled-pilot MVP. The dashboard and CSV evidence exports work today. Optional OTLP asset projection, a durable outbox, and a protobuf encoder exist; **network delivery to an OpenTelemetry Collector is still in development**. The [architecture guide](docs/architecture.md) describes the current boundaries.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. The core supports Python 3.9+; the optional MCP component requires Python 3.10+ and the `mcp` extra.

EdgeDisco was created by Neeraj Sabharwal and is licensed under [AGPLv3](LICENSE). [Website](https://edgedisco.com) · [Source](https://github.com/edgedisco/edgedisco)
