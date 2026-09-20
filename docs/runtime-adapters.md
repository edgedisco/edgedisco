# Runtime adapters

Runtime adapters capture agent lifecycle metadata from supported applications. They do not capture prompts, responses, source code, tool arguments, tool output, transcripts, credentials, email addresses, or raw workspace paths.

The dashboard's **Agent sessions** table is populated only after normalized hook or SDK events reach the server. Process scanning does not create sessions. This release installs native adapters for Cursor, Claude Code, and GitHub Copilot CLI; detected Codex/OpenCode processes do not imply an installed session adapter for those tools. A configured hook is not proof of invocation: verify a new native app session produces runtime events, then check its projected session. Sessions without activity for 15 minutes are displayed as stale.

## Install adapters

Run this after the endpoint is enrolled. Use an absolute configuration path:

```bash
ai-inventory adapters install --config "$(pwd)/agent.json" \
  --apps cursor claude-code github-copilot
```

The installer preserves existing JSON configuration and creates a one-time `.edgedisco.bak` backup before its first change.

## Cursor

EdgeDisco adds user-level hooks to `~/.cursor/hooks.json` for session start/end, successful or failed tool use, and subagent start/stop. Cursor watches this file and normally reloads it automatically. See the official [Cursor Hooks documentation](https://cursor.com/docs/hooks).

## Claude Code

EdgeDisco merges hooks into `~/.claude/settings.json` for sessions, tool use, subagents, and tasks. Start a new Claude Code session after installation. Use `/hooks` inside Claude Code to verify the hooks. See the official [Claude Code Hooks documentation](https://code.claude.com/docs/en/hooks-guide).

## GitHub Copilot

EdgeDisco writes personal hooks to `~/.copilot/hooks/edgedisco.json` for session and tool lifecycle events. Repository-managed hooks can be distributed through `.github/hooks/*.json`. See the official [GitHub Copilot Hooks documentation](https://docs.github.com/en/copilot/concepts/agents/hooks).

## Test without an application

Generate a safe synthetic Cursor session event:

```bash
printf '%s' '{"conversation_id":"edgedisco-test","model":"test-model"}' | \
  ai-inventory hook emit --config agent.json --app cursor \
  --event sessionStart --protocol cursor

ai-inventory agent send --config agent.json
```

Refresh the dashboard. A Cursor agent session should appear without any prompt or content fields.

## Generic Python SDK

```python
from ai_asset_inventory.sdk import EdgeDisco

edgedisco = EdgeDisco("agent.json")

with edgedisco.session(agent_type="research-agent", model="example-model") as session_id:
    edgedisco.emit(
        "postToolUse",
        session_id=session_id,
        tool_name="search",
        status="completed",
        duration_ms=125,
    )
```

The SDK writes to the same local event spool as native hooks. The endpoint collector sends the events using its device credential.

Administrators can export the aggregated session projection from `/api/v1/agent-sessions.csv` and the individual, sanitized event history from `/api/v1/runtime-events.csv`. Both exports include pseudonymous session, agent, workspace, and user hashes for correlation; they never contain the original identifiers or workspace paths.

## Offline behavior

Hooks only append normalized events to a local file. They do not make network requests and do not block the host agent if collection fails. The collector rotates the spool under a cross-platform file lock, uploads at most 1,000 events per batch, and relies on server-side event IDs for deduplication.
