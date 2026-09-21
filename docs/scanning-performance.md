# Scanning performance

EdgeDisco treats process state and installed/configured state as two different workloads.

## Incremental collector

The long-running agent defaults to:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `process_poll_interval_seconds` | 60 | Take one current-user process snapshot, check bounded static-root metadata, and compare normalized inventory state |
| `scan_interval_seconds` | 300 | Upload a full heartbeat even when inventory did not change |
| `static_scan_interval_seconds` | 900 | Force reconciliation of applications, CLI entry points, binary hashes, and MCP configuration |

Static evidence is cached in memory. Between full reconciliations, EdgeDisco checks metadata only for its bounded application roots, executable roots, editor-extension roots, and supported MCP configuration paths. A size, inode, modification-time, or change-time difference invalidates the cache immediately. An in-place application, CLI, or editor-extension update that does not modify a watched directory is caught by the periodic full reconciliation. Existing binary SHA-256 results have a second cache keyed by resolved path and file identity metadata, so unchanged executable bytes are not reread.

With default settings, a newly created CLI entry point normally changes its executable root and is detected on the next 60-second collection cycle. The 15-minute static interval is the guaranteed fallback for changes that leave watched root metadata unchanged.

Installer setup bypasses the long-running agent's in-memory cache: it performs a fresh complete inventory scan, uploads it before setup succeeds, and then restarts the agent. A supported installed CLI should therefore appear immediately after a successful install or upgrade; it does not need to be running.

Every process poll produces a normalized state digest. The agent uploads a complete snapshot immediately when this state changes. If it does not change, runtime-hook events are still flushed and inventory waits for the heartbeat. Complete snapshots are intentional: the server uses absence from a fresh report to mark a previously running asset stopped.

Collection, inventory upload, and runtime-spool upload run independently. The collector keeps only the newest complete snapshot in a one-slot queue; network requests and retry backoff do not delay the next process poll. Only successful inventory acknowledgements advance the change/heartbeat state. Queued observations retain their original timestamp and expire after the shorter of the heartbeat interval or 15 minutes. Runtime uploads have their own retry loop and durable spool. Collection itself remains synchronous, so an expensive static refresh can still lengthen a poll.

If process enumeration fails or returns malformed output, the collector skips the report instead of advertising an empty process set. Existing server inventory expires naturally; a source failure must not masquerade as confirmed process termination.

Example tuning for a more responsive endpoint:

```json
{
  "scan_interval_seconds": 300,
  "process_poll_interval_seconds": 15,
  "static_scan_interval_seconds": 900
}
```

Shorter process polling improves detection latency but runs `ps` or the Windows process query more often. Ten seconds is the supported minimum. Static refreshes should normally remain much less frequent because installation and configuration state changes slowly.

## Why polling remains authoritative

There is no single portable, unprivileged process-start stream:

- macOS `NSWorkspace` publishes application launch and termination notifications, but Apple notes that its launch notification omits background applications and `LSUIElement` applications. It is useful as a wake-up hint for GUI apps, not complete agent-process evidence. See [NSWorkspace application launch notifications](https://developer.apple.com/documentation/appkit/nsworkspace/didlaunchapplicationnotification).
- Apple's Endpoint Security API can observe process execution comprehensively, but it requires a restricted entitlement and a system extension; Apple's sample also requires explicit approval and Full Disk Access. That conflicts with EdgeDisco's no-prompt, least-privilege self-service model. See [Endpoint Security](https://developer.apple.com/documentation/EndpointSecurity) and [Apple's monitoring sample](https://developer.apple.com/documentation/endpointsecurity/monitoring-system-events-with-endpoint-security).
- Windows exposes process start/stop events through WMI or ETW. For example, [`Win32_ProcessStartTrace`](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/krnlprov/win32-processstarttrace) reports a process ID, parent, name, session, and SID. A future Windows-native helper could use this as a hint and then run the existing current-user reconciliation.
- Linux event approaches such as proc connectors, audit, or eBPF have deployment, capability, and distribution-specific tradeoffs. Polling the current user's process view remains the predictable baseline.

Future native watchers should only wake the incremental scanner early. Periodic polling must remain as reconciliation for missed events, collector restarts, race conditions, and processes that start and stop before metadata can be resolved.

## Further optimizations

Potential next steps, in priority order:

1. Add per-source timing counters and asset counts so tuning is based on measured endpoint cost.
2. Add jitter to fleet heartbeat scheduling to prevent synchronized upload bursts.
3. Persist the static cache across collector restarts with a version and fingerprint-library checksum. Revalidate all referenced file metadata before reuse.
4. Add optional platform-native wake-up helpers while retaining periodic reconciliation.
5. Add a delta transport only if server load requires it. Deltas need sequence numbers, durable acknowledgements, replay, and periodic full snapshots; otherwise a lost stop event leaves incorrect running state.
