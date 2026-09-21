# MCP inventory access and synchronization

EdgeDisco provides an optional, read-only MCP server for interactive compliance queries and
inventory synchronization. It does not expose prompts, responses, source code, credentials,
environment values, command arguments, configuration URLs, or raw filesystem paths.

## Install and run

MCP support requires Python 3.10 or newer. Choose the installation path that matches how
EdgeDisco was installed.

### Self-service installation

The self-service installer creates `~/.edgedisco/venv`, installs the `mcp` extra, and places an
`edgedisco` launcher in `~/.local/bin`. Do not activate that virtual environment. After the
installer has completed, run this command from any directory:

```shell
edgedisco mcp --db ~/.edgedisco/data/inventory.db --host 127.0.0.1 --port 8081
```

### Source checkout

For a checkout, run the local-extra install from the repository root. The `.[mcp]` form means
“this checkout plus its MCP extra”; it is different from installing the package name from an
index. The virtual environment may be activated, or its Python can be called explicitly:

```shell
cd /path/to/edgedisco
python3 --version  # must report 3.10 or newer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[mcp]'
edgedisco mcp --db ~/.edgedisco/data/inventory.db --host 127.0.0.1 --port 8081
```

Without activation, use `.venv/bin/python -m pip` for installation and `.venv/bin/edgedisco` to
run the server. The package-name form, `python -m pip install 'ai-asset-inventory[mcp]'`, works
from any directory only when that package is available from the configured package index.

The Streamable HTTP endpoint is `http://127.0.0.1:8081/mcp`. The service is stateless and returns
JSON responses. `--audit-log` selects a JSON Lines audit file; otherwise EdgeDisco writes
`mcp-audit.jsonl` next to the database.

The process accepts only `127.0.0.1`, `localhost`, or `::1` as its bind host. Loopback is a network
boundary, not user authentication: other processes running as the same host user may be able to
connect. Use a dedicated operating system account where local-user isolation matters.

## Tools

The query tools are intended for exploration and return at most 500 records:

- `get_compliance_summary()` returns fleet counts.
- `list_devices(limit?)` returns enrollment and last-contact metadata. Runtime event uploads can
  update device `last_seen`, so use `inventory_device_status` for scan freshness.
- `list_ai_assets(kind?, running_only?, limit?)` returns sanitized current assets.
- `list_running_agents(limit?)` returns fresh running agent-runtime evidence.
- `list_agent_sessions(status?, app?, limit?)` filters the effective status, including `stale`.
- `list_mcp_servers(limit?)` returns configured MCP servers without arguments, environment values,
  headers, credentials, or URLs.

Every asset result includes a `simulated` boolean. Simulated demo evidence also carries the fixed
label `SIMULATED TEST WORKLOADS`.

Use the paginated synchronization tools when a complete inventory is required:

- `inventory_snapshot(watermark?, after?, limit?)` returns at most 500 current asset records per
  page, including sanitized MCP configuration records. Begin without `watermark` or `after`. Reuse
  the returned `watermark` on every later page and pass `next_after` as `after` until it is null.
- `inventory_changes(cursor?, limit?)` returns ordered `upsert` and `delete` changes. Begin at the
  completed snapshot's watermark. Persist `next_cursor`, continue while `has_more` is true, and
  then poll from that cursor.
- `inventory_device_status(after?, limit?)` pages by stable device ID and reports the latest
  inventory observation and receipt times plus the current 15-minute freshness decision. Poll it
  separately from asset changes: an unchanged asset does not create another change event.

The snapshot watermark fixes a consistent point in the change history, so scans arriving during
pagination appear in the change feed. Asset records have a stable `source_asset_id` and contain
only allowlisted outbound fields. An absent asset produces a `delete` change. Simulated evidence
is explicitly labeled.

The change log is currently retained indefinitely. Consumers should persist their cursor and be
prepared to take a new snapshot if a future release introduces retention and reports an expired
cursor. On upgrade, retained inventory is seeded before new uploads are accepted. If multiple
historical scans share a timestamp and their current membership cannot be reconstructed, that
device is omitted until its next inventory upload.

## Audit and remote access

Each tool handler that executes is appended to the MCP audit log with its timestamp, tool,
arguments, outcome, result count, and a non-sensitive exception type for failures. Requests rejected
by HTTP or MCP schema validation before handler execution belong in transport/proxy logs. The MCP
audit log does not contain results or exception messages. Restrict and rotate it like other audit
data.

For a remote consumer, keep EdgeDisco bound to loopback and place it behind an authenticated HTTPS
reverse proxy. Restrict the network path, use a dedicated consumer credential, record the caller in
proxy audit logs, and rate-limit requests. The MCP SDK validates the HTTP `Host` header against
loopback names by default, so the proxy must rewrite `Host` to the loopback upstream value. Preserve
the original host in a forwarding header only if proxy policy needs it.

The MCP audit records do not contain proxy identity. Correlate them with proxy access logs using
timestamps. This feed maps EdgeDisco evidence into a consumer's asset model; it does not publish
directly into a third-party inventory API or provide prompt and response traces.
