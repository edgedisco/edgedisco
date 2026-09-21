# Changelog

All notable project changes are documented here.

## 0.5.0

- Added Hermes Agent, OpenClaw, Kimi Code, Kilo Code, Mistral Vibe, Crush, Junie CLI, Auggie, Devin CLI, and hash-verified Factory Droid detection.
- Hardened interpreter and package-runner classification with exact scoped-package matching, version-qualified package support, and prompt-text false-positive protections.
- Added Windows executable-path collection for process identity and Factory Droid verification; native Windows validation remains pending.
- Made installer reruns replace same-version managed packages so updated catalogs and detector code are deployed.
- Made local-checkout installs explicit and retained remote archive installation for stdin or standalone installer runs.
- Replaced raw database-open tracebacks with safe ownership, permission, file-type, and backup guidance while preserving unreadable databases.
- Expanded the privacy-safe asset and session CSV schemas, added an individual runtime-event export, and hardened spreadsheet downloads against formula injection.
- Added an optional read-only MCP server using the official MCP Python SDK.
- Added MCP tools for compliance counts, devices, AI assets, running agents, agent sessions, and discovered MCP servers.
- Added bounded query results and metadata-only response schemas.
- Added a JSONL audit trail for every MCP tool call.
- Restricted the built-in MCP listener to localhost; remote deployments must use an authenticated HTTPS reverse proxy.
- Preserved Python 3.9 support for the endpoint collector while requiring Python 3.10+ only for the optional MCP component.

## 0.4.0

- Added a one-command, no-sudo macOS installer.
- Added automatic credential generation, endpoint enrollment, initial inventory, and adapter detection.
- Added per-user LaunchAgents for the local server and collector so they start automatically at login.
- Added `edgedisco setup`, `edgedisco status`, and safe `edgedisco uninstall` commands.
- Added upgrade and repair behavior by rerunning the installer.
- Added tests that ensure credentials never appear in LaunchAgent property lists.

## 0.3.0

- Added native runtime adapters for Cursor, Claude Code, and GitHub Copilot.
- Added metadata-only agent session, subagent, tool, and MCP lifecycle capture.
- Added an offline-safe locked event spool and idempotent batch upload.
- Added a generic Python SDK for custom local agents.
- Added agent-session storage, live-session dashboard, and agent CSV export.
- Added a one-shot `agent send` command for testing and scheduled collection.
- Corrected quick-start token persistence and complete endpoint enrollment steps.

## 0.2.0

- Added active agent runtime discovery with host-application process lineage and aggregated instance counts.
- Added the active agent total to the central dashboard.
- Added GitHub-ready documentation, CI, release packaging, and contribution guidance.

## 0.1.1

- Added Python 3.9 compatibility for standard macOS Python installations.
- Preserved the zero-dependency runtime.

## 0.1.0

- Added cross-platform process and installed-application discovery.
- Added MCP configuration inventory with secret and argument exclusion.
- Added device enrollment and unique upload credentials.
- Added SQLite evidence storage, dashboard, and CSV export.
- Added Docker, macOS, Linux, and Windows deployment assets.
