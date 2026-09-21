# Detection catalog

EdgeDisco combines several narrow evidence sources. It does not recursively scan a user's home directory or the whole filesystem.

## What is scanned

| Source | Scope | What an observation means |
| --- | --- | --- |
| Desktop applications | Top-level entries in the platform's standard application directories | A supported application bundle or desktop entry was present |
| Agent CLI executables | Exact allowlisted names in a bounded set of common executable directories | A supported command entry point was present |
| Editor extensions | Exact extension or plugin identities in standard per-user VS Code-family and JetBrains plugin directories | A supported extension was installed; not necessarily enabled or used |
| Live processes | Processes owned by the current user, plus at most eight parent links | A matching process was visible during this scan |
| MCP configuration | Exact supported configuration-file locations | A named MCP server was configured; not necessarily running |
| Runtime adapters | Sanitized lifecycle records emitted by installed hooks or the SDK | A session, subagent, tool, or MCP lifecycle event occurred |

The executable directories are `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/.local/share/pnpm`, `~/.opencode/bin`, `~/bin`, `~/go/bin`, `/opt/homebrew/bin`, and `/usr/local/bin`. Windows also checks the user's npm and Programs locations, including one product-directory level under Programs and Antigravity's `%LOCALAPPDATA%\\agy\\bin`. Only exact allowlisted filenames are tested; there is no recursive traversal.

VS Code-family extension discovery checks only the top-level entries under `~/.vscode/extensions`, `~/.vscode-insiders/extensions`, `~/.cursor/extensions`, `~/.windsurf/extensions`, and `~/.vscode-oss/extensions`. It recognizes exact publisher and extension IDs for Claude Code, Codex, Cline, Continue, Kilo Code, Kimi Code, Augment, GitHub Copilot, Gemini Code Assist, and OpenCode. JetBrains discovery checks only product-level `plugins` directories beneath the platform's standard per-user JetBrains data root. Candidate Junie, Continue, and Kilo Code directories are confirmed from their bounded `plugin.xml` metadata, including a size-limited manifest inside a direct plugin JAR when necessary. Project directories, extension settings, prompts, credentials, and extension data are not read.

On Unix-like systems the process query asks the OS for only the collector user's processes. On Windows, the collector requests each process owner and retains only rows owned by the current username. Parent lineage is used to associate a runtime with a supported host application or outer agent harness.

## Agent CLI signatures

The executable and package signals below are stored in the versioned [`fingerprints.json`](../src/ai_asset_inventory/fingerprints.json) library and are based on first-party documentation or source repositories checked in September 2026. Product packaging changes over time, so this catalog should be revalidated as part of release maintenance.

| Product | Type | Executable signal | Corroborating package or path signal | First-party source |
| --- | --- | --- | --- | --- |
| Claude Code | Commercial | `claude` | `@anthropic-ai/claude-code` | [Anthropic installation guide](https://docs.anthropic.com/en/docs/claude-code/getting-started) |
| OpenAI Codex | Open source / commercial service | `codex` | `@openai/codex` | [OpenAI Codex repository](https://github.com/openai/codex) |
| Gemini CLI | Open source / commercial service | `gemini` | `@google/gemini-cli` | [Gemini CLI installation guide](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/installation.mdx) |
| Google Antigravity CLI | Commercial | `agy` | `~/.local/bin/agy` or `%LOCALAPPDATA%\\agy\\bin\\agy.exe` | [Google Antigravity getting started](https://antigravity.google/docs/getting-started?tab=cli) |
| Google Antigravity IDE | Commercial | `agy-ide` | Optional IDE command-line launcher | [Google Antigravity IDE codelab](https://codelabs.developers.google.com/getting-started-agy-ide) |
| Cursor Agent | Commercial | `cursor-agent` | `~/.local/bin/cursor-agent` | [Cursor CLI installation guide](https://docs.cursor.com/en/cli/installation) |
| GitHub Copilot CLI | Commercial | `copilot` | `@github/copilot` | [GitHub Copilot CLI quickstart](https://docs.github.com/en/copilot/get-started/cli-quickstart) |
| Cline | Open source | `cline` | npm package and CLI entry point `cline` | [Cline installation guide](https://github.com/cline/cline/blob/main/docs/getting-started/installing-cline.mdx) |
| Aider | Open source | `aider` | package marker `aider-chat` | [Aider repository](https://github.com/Aider-AI/aider) |
| OpenCode | Open source | `opencode` | `~/.opencode/bin/opencode`, `~/bin/opencode`, or package-manager entry point | [OpenCode repository](https://github.com/anomalyco/opencode) |
| Goose | Open source | `goose` | standalone CLI entry point | [Goose repository](https://github.com/aaif-goose/goose) |
| Continue CLI | Open source / commercial service | `cn` | `@continuedev` package path | [Continue CLI quickstart](https://docs.continue.dev/cli/quickstart) |
| Kiro CLI | Commercial | `kiro-cli` | standalone CLI entry point | [Kiro CLI command reference](https://kiro.dev/docs/cli/reference/cli-commands/) |
| Amp | Commercial | `amp` | `@ampcode/cli` | [Amp CLI guide](https://ampcode.com/docs/cli) |
| Qwen Code | Open source / commercial service | `qwen` | `@qwen-code/qwen-code` | [Qwen Code repository](https://github.com/QwenLM/qwen-code) |
| SWE-agent | Open source | `sweagent` | Python console entry point | [SWE-agent usage guide](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/hello_world.md) |

Desktop and framework signatures also cover Google Antigravity, Google Antigravity IDE, Kiro IDE, Kimi Code Desktop, Goose Desktop, OpenCode Desktop, OpenClaw companions, ChatGPT, Claude, Cursor, GitHub Copilot, Windsurf, Ollama, LM Studio, Jan, AnythingLLM, Open WebUI, LocalAI, Dify, CrewAI, AutoGen, LangGraph/LangChain, and supported MCP server forms. Antigravity's flagship desktop application, IDE, and CLI are distinct installable surfaces and are reported separately; Kiro IDE and Kiro CLI are likewise distinct. Gemini CLI remains separate from Gemini Code Assist.

Additional CLI coverage:

| Product | Type | Executable signals | Package evidence / source |
| --- | --- | --- | --- |
| Hermes Agent | Open source | `hermes` | `hermes_cli`, `hermes-agent`; [Nous Research](https://github.com/NousResearch/hermes-agent) |
| OpenClaw | Open source | `openclaw` | `node_modules/openclaw/`; [OpenClaw](https://github.com/openclaw/openclaw) |
| Kimi Code | Open source / commercial service | `kimi`, `kimi-cli`, `kimi-agent` | `@moonshot-ai/kimi-code`, legacy `kimi_cli`; [Kimi documentation](https://moonshotai.github.io/kimi-code/en/guides/getting-started) |
| Kilo Code | Open source / commercial service | `kilo` | `@kilocode/cli`; [Kilo repository](https://github.com/Kilo-Org/kilocode) |
| Mistral Vibe | Open source / commercial service | `vibe`, `vibe-acp` | `mistral-vibe`; [Mistral repository](https://github.com/mistralai/mistral-vibe) |
| Crush | Open source | `crush` | `@charmland/crush`; [Charm repository](https://github.com/charmbracelet/crush) |
| Junie CLI | Commercial | `junie` | Console entry point; [JetBrains documentation](https://junie.jetbrains.com/docs/junie-cli.html) |
| Auggie | Commercial | `auggie` | `@augmentcode/auggie`; [Augment documentation](https://www.augmentcode.com/product/CLI) |
| Devin CLI | Commercial | `devin` | Console entry point; [Cognition documentation](https://devin.ai/cli) |
| Factory Droid | Commercial | `droid` plus a published binary hash | [Factory installer and checksum provenance](https://app.factory.ai/cli) |

The catalog also includes `.exe` names. Factory Droid has stricter identification because unrelated software uses `droid`: both installed and running observations require a readable binary in an approved root matching a published Factory SHA-256. The initial hashes cover 0.223.0 for macOS ARM64/x64/x64-baseline, Linux ARM64/x64/x64-baseline, and Windows x64. Other versions, wrappers, bare process names without an absolute executable path, and unreadable binaries are skipped until corroborating evidence is available. No candidate executable is launched during discovery.

## Confidence and limitations

- Exact executable names are useful evidence but remain heuristic: an unrelated executable can reuse the same short name, and a renamed or wrapped agent may be missed.
- Generic Python, Node, `npx`, `uvx`, and Docker processes are classified only from their module, script, package, or image identity. Later arguments are not searched, preventing prompts or arbitrary command text from becoming detection signals.
- Editor extension installation is not equivalent to an enabled extension or an active agent session. Where an editor hides the agent behind its main process, a native adapter is needed for session-level evidence.
- A point-in-time process scan can miss very short-lived commands. Continuous collection and native hooks improve coverage.
- The long-running collector checks current-user processes and bounded static-root metadata every 60 seconds by default. A newly added CLI normally changes its executable directory metadata and is found on that cycle. A forced full static reconciliation runs every 15 minutes as a fallback for in-place changes that do not alter watched directory metadata. Installer setup performs an immediate full scan. Native runtime hooks can report supported session activity sooner.
- `running=true` means the process was observed in the latest fresh inventory snapshot. It does not assert that the agent was actively generating a response at that instant.

## Privacy treatment

Full paths and sanitized command structures are hashed locally. Reports include executable basenames but not process IDs, usernames, raw command lines, prompts, responses, environment values, MCP arguments, or configuration URLs. Sensitive flag values are removed before command fingerprints are calculated. Unknown processes are ignored.

Executable content is hashed only after a supported application or CLI is found in an approved install root. A symlink whose target escapes those roots is skipped. EdgeDisco does not broaden the filesystem search to find hashable files. See [Binary fingerprinting](fingerprinting.md).
