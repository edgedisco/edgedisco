# Native macOS enterprise architecture and deployment roadmap

EdgeDisco currently provides a per-user managed installation under `~/.edgedisco`, using LaunchAgents in the user's GUI domain. This roadmap defines the architecture, packaging, security boundaries, and implementation phases for an enterprise macOS version that can be deployed by administrators as root via MDM (Jamf Pro, Kandji, Mosyle, Microsoft Intune), run background components with appropriate privilege separation, present a native, lightweight menu bar status application, and expose on-demand MCP interfaces over stdio.

---

## 1. Architectural principles & privilege separation

Enterprise macOS software must respect Apple's modern security architecture: Transparency, Consent, and Control (TCC), System Integrity Protection (SIP), Gatekeeper, Hardened Runtime, and user session isolation.

A monolithic root daemon cannot cleanly handle local AI runtime inspection because developer tools (Cursor, Claude Code, GitHub Copilot) run inside the user's login session and write configuration and hooks inside the user's home directory. Furthermore, starting in macOS 12+, root is subject to TCC protections and cannot read protected user directories without explicit enterprise authorization.

EdgeDisco Enterprise uses a **three-tier architecture** with decoupled on-demand tooling:

```text
┌─────────────────────────────────────────────────────────────┐
│ 1. User Menu Bar Application (Swift / AppKit)               │
│    - Lives in /Applications/EdgeDisco.app                   │
│    - Runs in user GUI login session (LSUIElement)           │
│    - NSStatusItem toolbar icon with live status dot         │
│    - Actions: "Open Dashboard", "Scan Now", "Diagnostics"   │
└───────────────────────────┬─────────────────────────────────┘
                            │ IPC (Unix Domain Socket / Authenticated Loopback)
┌───────────────────────────▼─────────────────────────────────┐
│ 2. Per-User Sensor & Hook Agent (LaunchAgent)              │
│    - Runs as logged-in user in gui/<uid>                    │
│    - Owns local app hooks (.cursor, .claude, .copilot)      │
│    - Collects user-scoped process observations              │
└───────────────────────────┬─────────────────────────────────┘
                            │ Spool sync / Local authenticated socket
┌───────────────────────────▼─────────────────────────────────┐
│ 3. System Core Daemon (LaunchDaemon)                        │
│    - Runs as root / dedicated service user via LaunchDaemon │
│    - Manages central local SQLite datastore & OTLP outbox   │
│    - Serves loopback UI (127.0.0.1:8080)                    │
│    - Enforces MDM configuration policies                    │
└───────────────────────────┬─────────────────────────────────┘
                            │ Shared SQLite WAL (Read-Only)
┌───────────────────────────▼─────────────────────────────────┐
│ 4. Decoupled Stdio MCP Server (Python / TypeScript CLI)     │
│    - Spawned on-demand by developer IDEs over stdio         │
│    - Zero network listeners, zero persistent background RAM │
│    - Queries local inventory.db directly in read-only mode   │
└─────────────────────────────────────────────────────────────┘
```

### Component roles

| Component | Lifecycle | Execution Context | Responsibility |
| --- | --- | --- | --- |
| **Menu Bar App** | Login session | Logged-in user | Status visualization, menu actions, browser dashboard bootstrapping |
| **Per-User Agent** | LaunchAgent (`gui/<uid>`) | Logged-in user | Runtime hooks, user config detection, local event spooling |
| **System Daemon** | LaunchDaemon (`system`) | Root or `_edgedisco` | Central database, OTLP telemetry delivery, fleet server sync, host process inventory |
| **MCP Server** | On-demand child process | Invoking IDE process | Serves tools to Cursor, Claude Code, Windsurf via `stdio` (no network port) |

---

## 2. Decoupled on-demand MCP architecture (stdio)

The Model Context Protocol (MCP) server is **explicitly excluded from background system daemons and network listeners**:

1. **Protocol standard:** Modern MCP clients (Cursor, Claude Desktop, Claude Code, Windsurf) launch MCP servers as direct child subprocesses communicating over `stdin` and `stdout`.
2. **Network safety:** Running MCP as an HTTP/SSE network service opens unnecessary local TCP ports, risks port collisions, and creates firewall/proxy friction in enterprise developer environments.
3. **Resource efficiency:** An on-demand stdio process consumes memory only while the developer's IDE or agent session is active, terminating immediately when the parent process exits.
4. **Implementation flexibility:**
   - **Python stdio runner:** Lightweight `edgedisco mcp` command connecting directly to the local SQLite database in read-only mode (`?mode=ro`).
   - **TypeScript / Node.js package:** Standalone `@edgedisco/mcp` package distributed via npm / npx or bundled in the app, providing a clean TypeScript implementation for web/JS ecosystems.
   - Both implementations share the exact same underlying SQLite database (`/Library/Application Support/EdgeDisco/data/inventory.db`) and schema without requiring IPC to the daemon.

---

## 3. Architecture option spectrum

### Dimension A: User interface models

| Option | Mechanics | Memory | Pros | Cons |
| --- | --- | --- | --- | --- |
| **Option 1: Status Item → Default Browser (Tailscale Model)** *(Recommended)* | Pure Swift `NSStatusItem`. Menu items show quick stats; "Open Dashboard" launches Safari/Chrome to authenticated loopback URL. | 10–15 MB | Minimal memory footprint; 100% reuse of existing responsive web dashboard. | Opens a browser tab instead of an enclosed desktop window. |
| **Option 2: Native Menu Bar Popover (`WKWebView`)** | Clicking status icon drops down a native popover containing an embedded `WKWebView` rendering the dashboard. | ~40 MB (active) | Feels like an integrated desktop app (Docker Desktop / 1Password Mini model). | Higher memory usage while open; requires WebKit bridge handling. |
| **Option 3: Headless Daemon (Osquery / Datadog Model)** | No UI or menu bar presence. Background services report directly to enterprise OTLP / fleet server. | 0 MB (no UI) | Zero GUI maintenance; completely invisible to developers. | Developers cannot inspect local observations, verify privacy boundaries, or trigger manual scans. |

### Dimension B: Packaging & registration models

| Option | Mechanics | Target Audience | Pros | Cons |
| --- | --- | --- | --- | --- |
| **Option 1: Enterprise Flat `.pkg` with Postinstall** *(Recommended for IT)* | Signed component `.pkg` executed via `installer -target /`. `postinstall` script drops plists into `/Library/` and calls `launchctl`. | MDM (Jamf Pro, Kandji, Intune, Mosyle) | Industry standard for silent, unattended fleet-wide deployments. | Requires root installation privileges. |
| **Option 2: Modern `SMAppService` Drag-and-Drop `.app`** | App bundle embeds daemon and uses Apple's macOS 13+ `SMAppService` API to register LaunchDaemons/LaunchAgents. | Individual developers & self-service | Clean installation into `/Applications/`; native Ventura+ Login Items UI; clean drag-to-trash uninstall. | Less standard for legacy zero-touch MDM mass pushes without Service Management config profiles. |
| **Option 3: Dual-Mode Distribution** | Build signed `.app` bundle, then wrap into signed `.pkg` for MDM and `.dmg` for individual download. | Universal fleet + self-service | Accommodates both automated fleet deployment and voluntary developer adoption. | Dual packaging and release artifact pipeline. |

### Dimension C: Core engine runtime

| Option | Technology | Binary Size | Memory Footprint | Engineering Effort |
| --- | --- | --- | --- | --- |
| **Option 1: Bundled Python Mach-O** *(Recommended Phase 1)* | Bundle existing engine with `PyInstaller` or `python-build-standalone` into Mach-O executable. | ~45–60 MB | ~40–50 MB idle | Low: 100% reuse of existing detection catalog, fingerprint library, and OTLP encoders. |
| **Option 2: Compiled Go or Rust Daemon** | Port collector loop, process scanning, and OTLP pipeline to a compiled native binary. | ~10–15 MB | 8–12 MB idle | Medium: Requires re-implementing process scanner and OTLP proto serialization in Go/Rust. |
| **Option 3: Pure Swift Daemon** | Native Swift daemon using Darwin APIs (`libproc`, `sysctl`, `NSWorkspace`, XPC). | ~12–18 MB | 10–15 MB idle | High: Rewriting entire detection catalog in Swift; unified Swift codebase across UI and daemon. |

---

## 4. Native menu bar application details (Swift / AppKit)

The user interface follows the lightweight model:

- **Technology:** Pure Swift using AppKit (`NSStatusItem` / `NSMenu`). No Electron or heavy web view runtimes.
- **Resource Footprint:** Target idle memory under 20 MB; zero CPU utilization when idle.
- **Packaging:** `LSUIElement = true` in `Info.plist` (operates strictly as an accessory in the status bar without a Dock icon or main window).
- **Status Indications:**
  - Standard monochrome glyph for idle / healthy monitoring.
  - Accent badge for active scanning or OTLP synchronization.
  - Warning badge when the server is unreachable or MDM policy enrollment is pending.
- **Menu Actions:**
  - **Status summary:** Shows connected status, asset count, and last export timestamp.
  - **Open Dashboard:** Requests a fresh one-time authenticated bootstrap token and launches the user's default browser to `http://127.0.0.1:8080/browser-bootstrap/<token>`.
  - **Run Manual Scan:** Signals the sensor to run an immediate discovery pass.
  - **Device & Enterprise Info:** Displays local device ID, enrolled enterprise tenant, and version.
  - **Export Diagnostic Archive:** Bundles sanitized logs and status for IT support.

---

## 5. Packaging & deployment (.pkg vs .app)

### Deliverable format: Signed `.pkg` installer

Enterprise MDM systems cannot deploy bare `.app` bundles silently and reliably. Distribution requires a signed, notarized flat component package (`.pkg`).

```text
EdgeDisco-<version>.pkg
├── Distribution
├── Component Package (payload: /Applications/EdgeDisco.app)
└── Scripts
    ├── preinstall
    └── postinstall
```

### Filesystem layout

- `/Applications/EdgeDisco.app`:
  - `Contents/MacOS/EdgeDisco`: Native menu bar binary.
  - `Contents/Helpers/edgedisco-core`: Core engine executable (bundled Python standalone or compiled binary).
  - `Contents/Library/LaunchDaemons/com.edgedisco.daemon.plist`: System daemon configuration.
  - `Contents/Library/LaunchAgents/com.edgedisco.agent.plist`: Per-user agent configuration.
  - `Contents/Resources/`: Icons, assets, and web dashboard bundle.
- `/Library/Application Support/EdgeDisco/`:
  - `data/`: Central SQLite database (`inventory.db`) with `0755` directory permissions (`0644` WAL database for read-only MCP access).
  - `logs/`: System daemon logs.
  - `config/`: System-wide environment overrides and enrollment keys.
- `/Library/LaunchDaemons/com.edgedisco.daemon.plist`: Symlinked or copied by `postinstall`.
- `/Library/LaunchAgents/com.edgedisco.agent.plist`: Symlinked or copied by `postinstall`.

### Installer script lifecycle

1. **`preinstall`:**
   - Detects existing installations.
   - Quiesces active services via `launchctl bootout`.
   - Backs up existing database state if an upgrade is in progress.
2. **`postinstall`:**
   - Sets secure file permissions (`chown -R root:wheel /Applications/EdgeDisco.app`).
   - Ensures `/Library/Application Support/EdgeDisco` exists with restricted mode (`0750`).
   - Reads any MDM pre-stage configuration from `/Library/Managed Preferences/com.edgedisco.plist`.
   - Registers and starts the LaunchDaemon:
     ```bash
     launchctl bootstrap system /Library/LaunchDaemons/com.edgedisco.daemon.plist
     ```

---

## 6. Code signing, notarization, and TCC

### Apple Developer ID requirements

Building an enterprise macOS distribution requires an active Apple Developer account:

1. **Application Binaries:**
   - Signed with `Developer ID Application: <Legal Name> (<TEAM_ID>)`.
   - Hardened Runtime enabled (`--options runtime`).
   - Secure timestamp included (`--timestamp`).
2. **Package Installer:**
   - Signed with `Developer ID Installer: <Legal Name> (<TEAM_ID>)`.
3. **Notarization:**
   - Automated submission to Apple Notary Service via `xcrun notarytool submit`.
   - Ticket stapled to the final `.pkg` using `stapler staple`.

### Transparency, Consent, and Control (TCC)

To inspect local tool installations and process lineage without triggering user-facing permission dialogs:

- EdgeDisco restricts scans to standard executable search locations and current-user process tables.
- For enterprise fleet managers requiring scanning across non-standard developer paths, provide a pre-authored Privacy Preferences Policy Control (PPPC / `.mobileconfig`) profile granting Full Disk Access (FDA) to `/Applications/EdgeDisco.app/Contents/Helpers/edgedisco-core`.

---

## 7. MDM configuration & fleet policy

Enterprise settings are managed without editing shell files via standard Apple Managed Preferences:

- **Preference Domain:** `com.edgedisco.agent`
- **Managed Location:** `/Library/Managed Preferences/com.edgedisco.agent.plist`

Supported MDM payload keys:

| Key | Type | Description |
| --- | --- | --- |
| `CentralServerUrl` | String | Central inventory server endpoint (e.g. `https://inventory.corp.internal`) |
| `EnrollmentToken` | String | Shared or device-specific enrollment token |
| `OtlpEndpoint` | String | OpenTelemetry collector host and port (`host:port`) |
| `OtlpHeaders` | Dictionary | Authentication and routing headers for OTLP gRPC/HTTP |
| `ScanIntervalSeconds` | Integer | Minimum polling interval (clamped to supported limits) |
| `AllowedAdapters` | Array of Strings | Allowlisted runtime adapters (`cursor`, `claude-code`, `github-copilot`) |
| `DisableLocalDashboard` | Boolean | Disables local web UI port if central reporting is strictly enforced |

---

## 8. Implementation roadmap

### Phase 1: Self-contained engine bundling & LaunchDaemon packaging
- Package the existing Python engine into a self-contained Mach-O bundle using `PyInstaller` or `python-build-standalone`, eliminating external interpreter dependencies.
- Split system daemon (`com.edgedisco.daemon`) and user session collector (`com.edgedisco.agent`).
- Author flat `.pkg` with `postinstall` script and test silent installation via `sudo installer -pkg EdgeDisco.pkg -target /`.
- Wire Developer ID signing and notarization automation into CI.

### Phase 2: Native Swift menu bar application
- Implement lightweight AppKit `NSStatusItem` project in Swift.
- Establish authenticated local IPC between the menu bar app and the local daemon (Unix domain socket or loopback HTTP token).
- Implement dynamic status icons (healthy, syncing, warning) and menu actions (dashboard launch, scan trigger).
- Package `EdgeDisco.app` containing the menu bar executable and helper binaries.

### Phase 3: Decoupled stdio MCP tools (Python & TypeScript)
- Verify direct read-only SQLite access from `stdio` MCP processes.
- Implement standalone Python `edgedisco-mcp` stdio entrypoint.
- Author standalone TypeScript `@edgedisco/mcp` client package for npm / npx distribution.

### Phase 4: MDM managed preferences & enterprise validation
- Implement Managed Preferences parser reading `/Library/Managed Preferences/com.edgedisco.agent.plist`.
- Ship a downloadable reference `.mobileconfig` profile for MDM distribution.
- Test deployment across Jamf Pro and Microsoft Intune environments on clean macOS 13, 14, and 15 machines.
