# Native macOS enterprise architecture and deployment roadmap

EdgeDisco currently provides a per-user managed installation under `~/.edgedisco`, using LaunchAgents in the user's GUI domain. This roadmap defines the architecture, packaging, security boundaries, and implementation phases for an enterprise macOS version that can be deployed by administrators as root via MDM (Jamf Pro, Kandji, Mosyle, Microsoft Intune), run background components with appropriate privilege separation, present a native, lightweight menu bar status application, and report telemetry up to the enterprise central fleet server.

---

## 1. Architectural principles & privilege separation

Enterprise macOS software must respect Apple's modern security architecture: Transparency, Consent, and Control (TCC), System Integrity Protection (SIP), Gatekeeper, Hardened Runtime, and user session isolation.

A monolithic root daemon cannot cleanly handle local AI runtime inspection because developer tools (Cursor, Claude Code, GitHub Copilot) run inside the user's login session and write configuration and hooks inside the user's home directory. Furthermore, starting in macOS 12+, root is subject to TCC protections and cannot read protected user directories without explicit enterprise authorization.

EdgeDisco Enterprise uses a **three-tier endpoint architecture**:

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
│    - Exports telemetry upstream to Enterprise Fleet Server  │
└───────────────────────────┬─────────────────────────────────┘
                            │ OTLP / HTTPS Telemetry Sync
                            ▼
           [Enterprise Cloud / Fleet Server]
```

### Component roles

| Component | Lifecycle | Execution Context | Responsibility |
| --- | --- | --- | --- |
| **Menu Bar App** | Login session | Logged-in user | Status visualization, menu actions, browser dashboard bootstrapping |
| **Per-User Agent** | LaunchAgent (`gui/<uid>`) | Logged-in user | Runtime hooks, user config detection, local event spooling |
| **System Daemon** | LaunchDaemon (`system`) | Root or `_edgedisco` | Central database, OTLP telemetry delivery, fleet server sync, host process inventory |

---

## 2. Architectural boundary: Endpoint sensor vs. Enterprise Cloud MCP

A critical design boundary separates the local macOS endpoint package from the enterprise governance layer:

### Why the macOS endpoint has NO MCP server
1. **Sensor, not query API:** The endpoint package is strictly a discovery sensor and telemetry forwarder. Its sole responsibility is observing local processes, files, and configurations, buffering them locally, and shipping telemetry to the central enterprise server.
2. **Developer workflows do not query endpoint MCP:** Individual developers sitting at their workstations do not query an MCP server to ask what AI tools or models are installed on their own machine. Their IDEs and CLIs are the initiators of those workloads.
3. **Attack surface minimization & privilege isolation:** Omitting MCP from the endpoint avoids opening local query ports, exposing SQLite databases to non-privileged processes, or bundling unnecessary runtimes into the macOS package. Local database directories can remain strictly protected (`0700`/`0750` root-only access).

### Where MCP belongs: The Enterprise Cloud / Fleet Server
1. **Enterprise agents need fleet-wide context:** Compliance bots, security operations agents (SecOps), asset governance harnesses, and Chief-of-Staff orchestrators require fleet-wide visibility across hundreds or thousands of developer workstations.
2. **Network server implementation:** On the enterprise cloud / central server, EdgeDisco exposes an authenticated **network MCP server** (Streamable-HTTP / SSE) backed by the central aggregated inventory.
3. **Enterprise governance use cases:**
   - *"Which developer workstations in engineering are running unapproved local LLMs or Ollama models?"*
   - *"List all machines with MCP configurations connecting to non-allowlisted external endpoints."*
   - *"Audit fleet-wide AI tool adoption and security policy drift over the past 7 days."*
4. **IAM integration:** Network MCP at the enterprise tier integrates with enterprise identity providers (OIDC, SAML, mTLS, scoped API tokens) rather than local OS accounts.

---

## 3. Unified engine with dual deployment targets & cross-platform parity

EdgeDisco maintains a **single core discovery codebase** while offering two distinct deployment models tailored to developer evaluation versus enterprise production:

| Dimension | Unprivileged User-Managed (`~/.edgedisco`) | Enterprise Root Package (Production MDM) |
| --- | --- | --- |
| **Target User** | Individual developer, security researcher | Enterprise IT / SecOps fleet administrator |
| **Installation Command** | `curl -fsSL https://get.edgedisco.com | sh` or `pip install` | `sudo installer -pkg EdgeDisco.pkg -target /` via MDM |
| **Privilege Level** | Unprivileged (`gui/<uid>`) | Root / Local System (`system`) + User Agent (`gui/<uid>`) |
| **Installation Path** | `~/.edgedisco` | `/Applications/EdgeDisco.app` + `/Library/Application Support/EdgeDisco` |
| **Container & VM Access** | User-owned runtime sockets only | Direct access to `/var/run/docker.sock`, Podman, and VM hypervisors |
| **Tamper Resistance** | Low (developer can stop/remove services) | High (root-owned, MDM configuration profile protected) |
| **Telemetry Upstream** | Local dashboard / optional individual OTLP | Automated enterprise fleet server synchronization |
| **Lifecycle Commands** | `edgedisco start / stop / restart` | `edgedisco start / stop / restart --root` |

### Cross-platform architecture parity
This split establishes the blueprint across all supported endpoint operating systems:

- **macOS:** Signed, notarized flat `.pkg` installing a root `LaunchDaemon` (`system`) and a user `LaunchAgent` (`gui/<uid>`), accompanied by a native Swift menu bar application.
- **Linux:** Signed `.deb` / `.rpm` packages deploying a root `systemd` system service (privileged socket & process monitoring) alongside an unprivileged user session collector (`systemd --user`).
- **Windows:** Signed `.msi` deploying a native Windows Service running as `LOCAL SYSTEM` (monitoring processes and Docker named pipes) alongside a user session startup task.

---

## 4. Architecture option spectrum

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

## 5. Native menu bar application details (Swift / AppKit)

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

## 6. Packaging & deployment (.pkg vs .app)

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
  - `data/`: Central local SQLite database (`inventory.db`) and OTLP outbox buffer with restricted permissions (`0700`/`0750` root/service only; no unauthenticated local reads).
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

## 7. Code signing, notarization, and TCC

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

## 8. MDM configuration & fleet policy

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

## 9. Implementation roadmap

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

### Phase 3: MDM managed preferences & policy enforcement
- Implement Managed Preferences parser reading `/Library/Managed Preferences/com.edgedisco.agent.plist`.
- Ship a downloadable reference `.mobileconfig` profile for Jamf Pro, Kandji, and Intune distribution.
- Support enterprise policy keys (central server URL, enrollment tokens, OTLP endpoint/headers, adapter filtering).

### Phase 4: Enterprise fleet cloud integration & validation
- Validate telemetry delivery and outbox synchronization against the central EdgeDisco Cloud service.
- Verify that central cloud network MCP server (Streamable-HTTP / SSE) queries fleet inventory reported by macOS endpoints.
- Test silent MDM installation, upgrade, and uninstallation across clean macOS 13, 14, and 15 machines.
