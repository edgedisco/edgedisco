# Native Rust Core Engine Specification

## 1. Objectives & architectural goals

EdgeDisco is porting its endpoint discovery engine from Python to native Rust. This transition establishes a production-grade, enterprise-ready sensor designed for fleet-wide MDM deployment across macOS, Linux, and Windows.

### Core requirements
- **Minimal resource footprint:** Idle memory under **8 MB RSS**; zero CPU utilization between scan intervals.
- **Single static binary:** Zero external runtime dependencies (no Python interpreter, no dynamic C runtime mismatches).
- **Clean enterprise EDR profile:** Native compilation eliminates heuristic malware/unpacker flags common to PyInstaller and bundled script runtimes.
- **Memory & thread safety:** Compile-time memory safety for root services (`LaunchDaemon` on macOS, `systemd` on Linux, `SYSTEM` on Windows).
- **Cross-platform OS abstraction:** Direct bindings to native kernel/process APIs:
  - **macOS:** Darwin `libproc` and `sysctl` via native C-ABI.
  - **Linux:** High-performance `/proc` traversal and netlink listeners.
  - **Windows:** Win32 `CreateToolhelp32Snapshot` and process query APIs.
- **Zero OpenSSL dependency:** Use `rustls` for all TLS connections to ensure standalone portability across Linux distributions and macOS versions.

---

## 2. Workspace & crate architecture

The Rust implementation is structured as a modular Cargo workspace:

```text
edgedisco/
├── Cargo.toml                     # Workspace root manifest
├── crates/
│   ├── edgedisco-core/            # Domain models, detection rules, SQLite store
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── catalog.rs         # Allowlisted AI tool & model signatures
│   │       ├── models.rs          # Asset, Device, Session, Event structures
│   │       ├── store.rs           # SQLite inventory database & outbox buffer
│   │       └── redaction.rs       # Field allowlists & sanitization filters
│   │
│   ├── edgedisco-sensor/          # OS-specific process and runtime scanning
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── process/
│   │       │   ├── mod.rs         # Abstract process scanner trait
│   │       │   ├── darwin.rs      # macOS libproc / sysctl implementation
│   │       │   ├── linux.rs       # Linux /proc scanner
│   │       │   └── windows.rs     # Windows Win32 toolhelp scanner
│   │       └── container/
│   │           ├── mod.rs         # Container discovery interface
│   │           └── docker.rs      # Docker Engine socket client (/containers/json, top)
│   │
│   └── edgedisco-cli/             # Main executable binary
│       ├── Cargo.toml
│       └── src/
│           ├── main.rs
│           ├── ipc.rs               # Restricted NDJSON-over-Unix-socket local IPC
│           ├── commands/            # Subcommand handlers (scan, status, start, stop, restart)
│           ├── daemon.rs            # Background collector loop & scheduler
│           └── service/             # OS service manager (launchctl, systemctl, win service)
```

---

## 3. Technology stack & dependencies

All dependencies are vetted for minimal footprint, memory safety, and cross-compilation support:

| Domain | Crate | Purpose | Justification |
| --- | --- | --- | --- |
| **CLI & Flags** | `clap` (derive) | Command-line argument parsing | Modern, type-safe, zero boilerplate |
| **Serialization** | `serde`, `serde_json` | JSON serialization/deserialization | Industry standard, zero-copy deserialization |
| **Database** | `rusqlite` (`bundled`) | Local SQLite inventory & outbox | Embedded SQLite with WAL mode, zero external C library requirements |
| **Async Runtime** | `tokio` (macros, rt-multi-thread) | Background daemon scheduling | High efficiency, low idle overhead |
| **HTTP & TLS** | `reqwest` (`rustls-tls`, default-features=false) | OTLP & API telemetry upload | Avoids system OpenSSL linking issues |
| **Logging & Tracing**| `tracing`, `tracing-subscriber` | Structured logging | Zero overhead when disabled, structured JSON/fmt output |
| **macOS Native** | `libc`, `mach2` | Darwin `libproc` / `sysctl` | Zero-cost C-ABI bindings to macOS kernel APIs |

---

## 4. Discovery engine & catalog parity

The Rust implementation strictly adheres to the existing Python detection and privacy contracts:

### 1. Process scanning invariants
- **Strict Allowlisting:** Only processes matching known AI signatures (e.g. `cursor`, `code`, `ollama`, `vllm`, `claude`, `hermes`) are cataloged.
- **Privacy Boundary:** Never collect process command-line arguments, environment variables, user documents, or prompts.
- **Lineage:** Record parent process IDs (`ppid`) locally to resolve agent runtimes spawned by developer IDEs.

### 2. Container scanning invariants (Root Mode)
- Connect to `/var/run/docker.sock` or user hypervisor sockets via Unix domain socket.
- Query `/containers/json` for image SHA-256 digests.
- Query `/containers/{id}/top` for running executable basenames only.
- **Never** read `Config.Env` or inspect mounted volumes.

### 3. SQLite local store schema
The SQLite schema matches the existing database layout:
- `devices`: Local device enrollment, hostname, platform, OS version, last seen.
- `assets`: Discovered AI models, developer tools, MCP servers, and agent runtimes.
- `sessions`: Active and historical agent execution sessions.
- `outbox`: Structured telemetry events queued for OTLP export.

---

## 5. CLI interface parity

The native binary `edgedisco` retains CLI compatibility with the existing Python tool:

```shell
# Instant one-off local scan (outputs JSON)
edgedisco scan

# Formatted status summary
edgedisco status

# Native service lifecycle (LaunchDaemon or systemd)
edgedisco start [--root]
edgedisco stop [--root]
edgedisco restart [--root]

# Background collector daemon with restricted local Unix-socket IPC
edgedisco daemon [--interval <seconds>] [--config <path>]
edgedisco daemon [--ipc-socket <path>] [--ipc-mode user|system]
```

The daemon IPC contract, filesystem and peer-credential policy, framing limits, lifecycle, and privacy boundary are specified in [`local-ipc.md`](local-ipc.md). The endpoint opens no TCP listener.

---

## 6. Implementation phases & quality gates

### Phase 1: Core domain & detection catalog
- Initialize Cargo workspace.
- Implement `edgedisco-core` models, detection catalog, and SQLite storage with WAL mode.
- Port unit tests from Python test suite.

### Phase 2: Native Darwin & Linux process sensors
- Implement `edgedisco-sensor` using Darwin `libproc` APIs.
- Implement Linux `/proc` scanner.
- Verify memory consumption under 8 MB RSS during active scan.

### Phase 3: CLI, lifecycle & service commands
- Implement `edgedisco-cli` with `clap`.
- Implement `launchctl` service management for macOS.
- Implement `systemctl` service management for Linux.

### Phase 4: Container discovery & OTLP export
- Implement Docker Engine socket client for container digest and process top inspection.
- Implement OTLP telemetry outbox exporter using `reqwest` / `rustls`.

### Quality gates
Every phase must pass:
1. `cargo fmt --check`
2. `cargo clippy --all-targets --all-features -- -D warnings` (Zero Warnings Policy)
3. `cargo test --all-targets --all-features`
