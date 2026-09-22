# Native macOS inventory and settings delivery plan

## Outcome and current boundaries

One SwiftUI application will provide a compact menu-bar summary, a searchable
inventory window, Settings, and diagnostics. The native collector must first regain
the Python inventory coverage that makes installed but idle tools visible.

Current evidence: Rust collects processes, containers, installed CLIs, bounded
macOS app bundles, VS Code-compatible extensions, MCP configuration, and known
JetBrains plugins. User and system daemons
expose separate inventory scopes. User settings support live updates; system
settings still require administrator-managed configuration and restart.

## 1. Discovery parity

- First slice: bounded installed CLI inventory using the shared fingerprint catalog,
  merged into native scan reports. Do not execute candidate tools or crawl homes.
- Preserve separate installed and process evidence; an idle installed CLI is present
  and not running. Do not treat these evidence rows as distinct product counts.
- Follow with macOS application bundles, editor extensions, and MCP declarations,
  using Python fixtures as the compatibility baseline. Preserve privacy boundaries.
- Acceptance: idle tools appear; unrelated files and symlink escapes do not; names
  containing spaces and supported package-manager links work; reports validate;
  persistence preserves installation evidence after the process stops.

## 2. User-session visibility and accurate counts

- Run a per-user native daemon in the login session so it serves the user's own
  inventory through authenticated local IPC. Keep privileged machine inventory
  separate. Do not make user databases writable by the system inventory reader.
- Extend the UI to query both sources explicitly and label scope and freshness.
  Deduplicate product summaries while retaining source evidence in details.
- Scan Now must target the displayed scope; a failed source remains visibly failed
  rather than being replaced by zero counts. Separate installed, running, and
  historical totals from the number of observations in the last scan.
- Acceptance: a user-home-only CLI and user-local container appear in the UI;
  system findings remain visible; one inaccessible source does not hide the other;
  logout/login, daemon restart, and package upgrade preserve correct operation.

## 3. Native inventory window

- Open a reusable, resizable window from the existing menu-bar app.
- Add search, type/state/scope filters, and details for version, evidence source,
  running/present state, and last seen. Keep the popover a short status summary.
- Acceptance: keyboard navigation, empty/error/loading states, large inventories,
  multiple opens without duplicate windows, and closing the window without quitting
  the menu-bar app. Verify layout on macOS with real packaged metadata.

## 4. Settings and live reload

- Expose interval, export enablement, OTLP endpoint, and batch size through typed IPC.
  Read access does not authorize system configuration writes. User settings are
  scoped to their daemon; system changes require administrator authorization.
- Validate before saving; atomically persist settings and apply them in the daemon.
  Reject stale concurrent updates and retain the last valid configuration on failure.
- Test Connection is an explicit, labeled test request. Store future credentials in
  Keychain or an equivalently protected service store; never return secret values.
- Acceptance: valid updates survive restart; invalid settings change neither disk
  nor active state; unauthorized writes fail; interrupted saves preserve valid JSON;
  applying settings does not falsely report successful telemetry delivery.

## 5. Export diagnostics and release gate

- Show queued/delivered/retried/failed counts and last successful delivery, plus
  service, configuration, and scan errors. Redact credentials and raw user content.
- Exercise an upgrade from the previous package with customized settings and a
  populated database. Verify both daemon scopes, UI counts, migration backups,
  readiness, and documented manual recovery.
- Run Rust, Swift, and macOS package suites; review the final diff before release.

## Execution record

- Implemented milestone 1's installed-CLI slice and wired it into native scans.
  It matches Python's macOS executable roots; broader package-manager layouts
  remain future work. Reads for binary evidence are capped at 256 MiB; an unverified
  Factory Droid binary is excluded to avoid the unrelated DROID name collision.
- Added fixture checks for idle tools, unrelated files, allowed package links,
  symlink escapes, removal, privacy validation, and installed-state persistence
  after a running process disappears.
- Local comparison on 2026-09-22: Python's installed-CLI collector and the updated
  native non-persisting scan both reported GitHub Copilot, Google Antigravity CLI,
  Hermes Agent, and OpenCode. This establishes the first slice on this machine,
  not full static-inventory parity or installed-app UI delivery.
- Continued with milestone 2: the packaged LaunchAgent now runs a persistent
  user-mode daemon. The UI explicitly selects My Session or This Mac, targets
  that source for scans, and discards late responses after switching scopes.
  This ships separate source views; a combined deduplicated product summary is
  still pending. The user service change takes effect on package installation.
- Implemented the initial milestone 3 inventory window: reusable and resizable,
  with search, state filters, versions, evidence types, and last-seen timestamps.
  Detection refresh failures retain an explicit stale-data warning. The popover
  labels observation counts as Last scan findings.
- Added per-scope Products and Evidence views. Products combine installation and
  runtime findings only for exact canonical catalog identities; unknown findings
  remain separate. The evidence view retains every record.
- The inventory window now has an Overview tab that reads both daemon scopes,
  combines catalog-matched products across them, and labels each source's
  findings or failure. A failed scope never becomes a misleading zero total.
  It now filters to either source or both and shows deduplicated installed,
  running, and previously-seen grouped-entry totals. Installed and running can
  overlap. Unknown findings with coincident IDs across scopes remain separate.
- Added a bounded macOS app-bundle slice: direct `.app` names in `/Applications`
  and the user's Applications folder are matched to exact catalog names/aliases.
  Symlinks and nested folders are skipped. The native scanner now reads at most
  256 KiB of `Contents/Info.plist` through no-follow directory handles and uses
  `CFBundleShortVersionString`, then `CFBundleVersion`, for XML or binary plists.
  Invalid, oversized, or linked metadata leaves the version unknown without
  hiding the detected app. This remains narrower than Python's installed-app inventory.
- Added VS Code-compatible extension inventory for the five Python-supported
  per-user roots (VS Code, Insiders, Cursor, Windsurf, VSCodium). Native scans
  use exact catalog IDs from bounded `package.json` files and respect
  `.obsolete`; symlinked folders/manifests, nested extensions, and oversized
  metadata are skipped. Per-root manifest reads stop after a 16 MiB budget.
  No extension code is executed or raw paths exported.
- Added bounded, no-follow MCP declaration discovery for Claude Desktop, Cursor,
  and VS Code configuration. Only server names, host app, transport type, and
  executable basename are exposed; URLs, arguments, environment, and raw paths
  are not exported.
- Added conservative direct and JAR-backed `plugin.xml` JetBrains plugin discovery
  for Junie, Continue, and Kilo Code. Archive and extracted XML reads are bounded.
- Added bounded, no-follow SHA-256 evidence for macOS bundle executables named
  in `Info.plist`. Unreadable, symlinked, and oversized executables leave the
  app visible without binary evidence.
- Read-only OTLP export diagnostics now report queued records, cumulative
  delivered/retried/failed/dropped counts, and last success/failure timestamps
  through scope-specific IPC and the Settings window. They expose no payloads,
  endpoints, or credentials.
- Added an explicit saved-endpoint OTLP connection probe. It sends an empty
  request using configured transport and authentication, checks a bounded OTLP
  response, and never claims outbox rows or records a delivery. HTTP status and
  coarse failure categories are shown without collector response bodies.
- Remaining: interactive window QA and login/upgrade validation. No installed
  services have been changed.
- The native Settings window now reads either daemon's effective configuration.
  My Session settings are stored at `~/.edgedisco/config/daemon.json`, with
  interval, OTLP enablement, endpoint, and batch size applied live after an
  atomic save. Updates carry a revision; stale and invalid writes are rejected.
  This Mac is read-only in the UI: its administrator-managed configuration still
  requires a service restart. Saving an endpoint is not evidence that export
  succeeded. The explicit empty-request connection probe does not prove telemetry
  delivery; cumulative export diagnostics are reported separately.
  Existing private OTLP headers, compression, timeout, and TLS file settings
  are preserved across UI saves but are never returned over the Settings IPC.
- Continuation checks: 10 menu-bar tests, 16 IPC tests, and 18 package tests passed.
- Validation: full Rust workspace suite passed; Clippy with warnings denied,
  formatting, and diff checks passed. No installed services were changed.
