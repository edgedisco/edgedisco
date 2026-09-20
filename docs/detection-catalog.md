# Detection catalog

EdgeDisco combines several narrow evidence sources. It does not recursively scan a user's home directory or the whole filesystem.

## What is scanned

| Source | Scope | What an observation means |
| --- | --- | --- |
| Desktop applications | Top-level entries in the platform's standard application directories | A supported application bundle or desktop entry was present |
| Agent CLI executables | Exact allowlisted names in a bounded set of common executable directories | A supported command entry point was present |
| Live processes | Processes owned by the current user, plus at most eight parent links | A matching process was visible during this scan |
| MCP configuration | Exact supported configuration-file locations | A named MCP server was configured; not necessarily running |
| Runtime adapters | Sanitized lifecycle records emitted by installed hooks or the SDK | A session, subagent, tool, or MCP lifecycle event occurred |

The executable directories are `~/.local/bin`, `~/.cargo/bin`, `~/.bun/bin`, `~/.local/share/pnpm`, `~/.opencode/bin`, `~/bin`, `~/go/bin`, `/opt/homebrew/bin`, and `/usr/local/bin`. Windows also checks the user's npm and Programs locations, including one product-directory level under Programs. Only exact allowlisted filenames are tested; there is no recursive traversal.

On Unix-like systems the process query asks the OS for only the collector user's processes. On Windows, the collector requests each process owner and retains only rows owned by the current username. Parent lineage is used to associate a runtime with a supported host application or outer agent harness.

## Agent CLI signatures

The executable and package signals below are stored in the versioned [`fingerprints.json`](../src/ai_asset_inventory/fingerprints.json) library and are based on first-party documentation or source repositories checked in September 2026. Product packaging changes over time, so this catalog should be revalidated as part of release maintenance.

| Product | Type | Executable signal | Corroborating package or path signal | First-party source |
| --- | --- | --- | --- | --- |
| Claude Code | Commercial | `claude` | `@anthropic-ai/claude-code` | [Anthropic installation guide](https://docs.anthropic.com/en/docs/claude-code/getting-started) |
| OpenAI Codex | Open source / commercial service | `codex` | `@openai/codex` | [OpenAI Codex repository](https://github.com/openai/codex) |
| Gemini CLI | Open source / commercial service | `gemini` | `@google/gemini-cli` | [Gemini CLI installation guide](https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/installation.mdx) |
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

Desktop and framework signatures also cover ChatGPT, Claude, Cursor, GitHub Copilot, Windsurf, Ollama, LM Studio, Jan, AnythingLLM, Open WebUI, LocalAI, Dify, CrewAI, AutoGen, LangGraph/LangChain, and supported MCP server forms.

## Confidence and limitations

- Exact executable names are useful evidence but remain heuristic: an unrelated executable can reuse the same short name, and a renamed or wrapped agent may be missed.
- Generic Python, Node, `npx`, `uvx`, and Docker processes are classified only from their module, script, package, or image identity. Later arguments are not searched, preventing prompts or arbitrary command text from becoming detection signals.
- Editor extension installation is not equivalent to an active agent session. Where an editor hides the agent behind its main process, a native adapter is needed for session-level evidence.
- A point-in-time process scan can miss very short-lived commands. Continuous collection and native hooks improve coverage.
- The long-running collector polls current-user processes independently from its cached static scan. Default detection latency is therefore up to 60 seconds for an uninstrumented process; native runtime hooks can report supported session activity sooner.
- `running=true` means the process was observed in the latest fresh inventory snapshot. It does not assert that the agent was actively generating a response at that instant.

## Privacy treatment

Full paths and sanitized command structures are hashed locally. Reports include executable basenames but not process IDs, usernames, raw command lines, prompts, responses, environment values, MCP arguments, or configuration URLs. Sensitive flag values are removed before command fingerprints are calculated. Unknown processes are ignored.

Executable content is hashed only after a supported application or CLI is found in an approved install root. A symlink whose target escapes those roots is skipped. EdgeDisco does not broaden the filesystem search to find hashable files. See [Binary fingerprinting](fingerprinting.md).
