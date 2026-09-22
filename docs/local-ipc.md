# Local Unix-domain-socket IPC

The native `edgedisco daemon` exposes a deliberately small local control plane over a Unix-domain socket. It does not start HTTP, WebSocket, MCP, loopback TCP, or any other network listener.

## Deployment modes

User mode is the default:

```text
edgedisco daemon
edgedisco daemon --ipc-socket /private/test/edgedisco.sock
```

The default path is `~/.edgedisco/edgedisco.sock`. The daemon creates the parent directory when needed, enforces mode `0700` on that directory, and sets the socket to mode `0600`. A connecting peer must have the daemon's effective UID or UID 0.

System mode is explicit:

```text
edgedisco daemon \
  --ipc-mode system \
  --ipc-socket /var/run/edgedisco.sock \
  --ipc-allowed-uid 501 \
  --ipc-owner-uid 0 \
  --ipc-group-gid 80
```

The system default path is `/var/run/edgedisco.sock` and its mode is `0660`. At least one numeric `--ipc-allowed-uid` is required. UID 0 and configured UIDs are authorized. Optional numeric owner/group values are supplied by installation configuration; EdgeDisco does not hard-code account or group names. Owner and group options must be supplied together. Filesystem permissions and peer credentials are both enforced; neither is treated as sufficient alone.

On macOS/BSD, the daemon reads peer UID/GID with `getpeereid`. On Linux it reads UID/GID/PID with `SO_PEERCRED`. Platforms without a supported peer-credential API fail closed.

## Socket lifecycle

Before binding, the daemon checks an existing path with `lstat` semantics. It removes the path only when it is a Unix socket owned by the daemon's current effective user and is not accepting connections. An active listener, regular file, symlink, or socket owned by another UID is never removed. This makes stale-socket recovery safe without turning the socket path into an arbitrary-file deletion primitive or displacing a running daemon.

After binding, the daemon records the socket device and inode. Shutdown stops accepts, waits up to 5 seconds for bounded in-flight client tasks, aborts any remaining tasks, closes the listener, and removes the path only if it is still the same socket. A path replaced while the daemon is running is not removed.

## Framing and limits

IPC is newline-delimited JSON (NDJSON). Each connection carries exactly one request and one response, then closes. UTF-8 JSON must be terminated by `\n`.

Default limits are:

| Limit | Value |
| --- | ---: |
| Request frame | 16 KiB, including newline |
| Response frame | 256 KiB, including newline |
| Concurrent clients | 8 |
| Request read timeout | 2 seconds |
| Response write timeout | 2 seconds |
| Explicit scan timeout | 120 seconds |
| Shutdown drain timeout | 5 seconds |

Connections above the concurrency limit are closed. Malformed JSON, partial frames, oversized frames, unsupported protocol versions, and unknown methods return a bounded error where possible and then close. A slow or disconnected client runs in its own bounded task and does not own the daemon's periodic scan loop.

## Protocol version 1

Request envelope:

```json
{"protocol_version":1,"request_id":"client-generated-id","method":"status"}
```

`request_id` is required and contains 1–128 bytes. Unknown request fields are rejected. The response envelope is:

```json
{"protocol_version":1,"request_id":"client-generated-id","ok":true,"result":{}}
```

Errors use this shape:

```json
{"protocol_version":1,"request_id":"client-generated-id","ok":false,"error":{"code":"unknown_method","message":"method is not exposed by the local IPC service"}}
```

The public Rust wire types are `IpcRequest`, `IpcResponse`, `ProtocolError`, and `SanitizedDetection` in `edgedisco_cli::ipc`.

### `negotiate`

Returns the selected and supported protocol versions:

```json
{"protocol_version":1,"supported_versions":[1]}
```

### `status`

Returns only daemon health and aggregate local state:

```json
{
  "healthy": true,
  "started_at": "2026-09-22T00:00:00Z",
  "last_scan_at": "2026-09-22T00:01:00Z",
  "last_scan_asset_count": 3,
  "device_count": 1,
  "detection_count": 3
}
```

### `detections`

Returns a list whose fields are limited to `kind`, `name`, `vendor`, `version`, `running`, `present`, and `last_seen`. The IPC serializer never includes device tokens, fingerprints, path or command hashes, binary hashes, metadata, environment variables, prompts, source paths, container mounts, secrets, or arbitrary database fields.

### `scan`

Enqueues one explicit scan on the daemon-owned scan channel and waits for that shipped scan path to finish:

```json
{"accepted":true,"asset_count":3}
```

The IPC module does not implement a second scanner or persistence path. Both periodic and explicit scans call the daemon's existing sensor/catalog/store iteration, and SQLite remains the owner of persisted detections.

## Threat boundary

This interface is a local, authenticated projection for a future native menu-bar client. It is not a general query API. It exposes no arbitrary SQL, filesystem reads, command execution, environment access, unrestricted dispatch, cloud/fleet query, or diagnostic archive functionality. Local root can connect by policy; protecting the host from root is outside this boundary. A process already running as an authorized UID can access the same user-scoped data and is inside the trust boundary.
