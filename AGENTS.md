# Agent guidance

## Scope and sources of truth

These instructions apply throughout this repository. Read the current diff before
editing and preserve unrelated work. For a review-only request, report actionable
findings with file locations and evidence; implement fixes when requested.

Rust is the active implementation, with a native Swift macOS client. The Python
application remains for legacy deployments and compatibility. Identify which
implementation a task concerns before changing commands, configuration, or docs.
Use current source, tests, `Makefile`, and `.github/workflows/` to verify behavior;
older guides and roadmap entries can lag implementation.

## Repository map

- `crates/edgedisco-core/`: catalog, models, redaction, SQLite store/migrations,
  OTLP encoding, and durable outbox delivery.
- `crates/edgedisco-sensor/`: platform process scanning, installed software
  discovery, container discovery, and evidence classification.
- `crates/edgedisco-cli/`: native CLI, daemon orchestration, persistent settings,
  local IPC, and service lifecycle.
- `macos/EdgeDiscoIPC/`: Swift wire types and async Unix-socket client.
- `macos/EdgeDiscoMenuBar/`: SwiftUI/AppKit application and presentation state.
- `packaging/macos/`: native installer, upgrade/uninstall logic, and package tests.
- `src/ai_asset_inventory/` and `tests/`: legacy Python application and tests;
  `tests/fixtures/golden_otlp/` also supplies native wire-compatibility fixtures.
- `docs/`: architecture, deployment, IPC, privacy, and OTLP contracts. Start with
  `docs/README.md`, `docs/local-ipc.md`, and
  `docs/spec/SPEC-001-OTLP-INVARIANTS.md` as appropriate.

## Development and validation

Use the repository root for these commands:

| Change | Validation |
| --- | --- |
| Rust behavior | `make check` (workspace tests, rustfmt, Clippy with warnings denied) |
| Native release build | `make build-rust` |
| Swift client or UI, on macOS | `make swift-test` (both Swift packages) |
| macOS packaging | `make macos-package-check` (requires pytest in the selected Python environment) |
| Legacy Python | `make check-python` |
| Documentation only | Check referenced paths/commands and run `git diff --check` |

`make test`, `make check`, and `make build` target Rust. They do not validate the
Python implementation. `make` prefers `.venv/bin/python` when present; otherwise
set `PYTHON` explicitly. Preserve Python 3.9 compatibility in the legacy core.

Add focused regressions for behavior changes and reproduced bugs. For detection
rules, cover positive evidence and likely false positives. After fixes, review
the final diff and run the complete affected suite, not only the new test. Avoid
repeating unchanged passing checks unless a new change or concern warrants it.
Report the commands run, failures, and any validation not completed.

Some Rust integration tests scan the live host and can take tens of seconds.
Socket tests need local socket access; SwiftPM needs writable build caches.
Distinguish sandbox/cache/network restrictions from product failures rather than
weakening tests to make a restricted run pass.

## Privacy and correctness boundaries

- Keep collection allowlisted and evidence-based. Preserve the distinction
  between installed, configured, running, observed, and simulated evidence.
- Do not capture prompts, responses, source/document contents, credentials,
  environment values, raw command lines, or tool inputs/outputs. Preserve the
  existing redaction and hashing boundaries in persistence and exported data.
- For new collected fields, document purpose, source, retention, whether raw
  values leave the endpoint, and redaction behavior. Follow the maintainer-review
  requirement for privacy expansion in `CONTRIBUTING.md`.
- Keep native daemon control on authenticated Unix sockets. Preserve peer
  credentials, filesystem permissions, safe stale-socket handling, and the
  explicit sanitized IPC projection. Do not expose arbitrary database fields.
- Keep Rust and Swift wire contracts aligned: version and request-ID validation,
  newline framing, size bounds, timeouts, and bounded failures. Validate socket
  path lengths in UTF-8 bytes before constructing a Unix-socket address.
- Keep UI state on the main actor and socket I/O asynchronous. Test late responses
  after scope changes and use async synchronization in tests rather than blocking
  semaphores inside async callbacks. Parse numeric settings strictly; formatted
  numeric parsing can silently accept fractions or trailing junk.
- Preserve migration rollback/backup behavior and outbox deduplication, lease
  ownership, bounded retention, and retry semantics. Check native OTLP output
  against the golden fixtures when changing its schema or encoding.
- A successful build or setup command is not proof of runtime health or telemetry
  delivery. Verify the relevant daemon, scope, export configuration, and actual
  queued/delivered result before making operational claims.

## Change delivery

Keep changes focused and update relevant docs when behavior changes. Use temporary
databases, sockets, and test collectors for validation; a source-code task does
not itself authorize changing an installed service or live export destination.
Do not include credentials, host inventory, private logs, databases, build output,
or ignored artifacts in commits. `Cargo.lock` is currently ignored; do not
force-add it as an incidental part of another change.

Commit and push only when requested. When authorized, inspect the final tree,
stage only intended files, verify the commit and working-tree state, and check
the remote before pushing. Do not force-push or overwrite unrelated work. Report
the commit and push outcome accurately, including anything still pending.
