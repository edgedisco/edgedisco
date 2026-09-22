# Native macOS enterprise architecture and deployment roadmap

EdgeDisco currently provides a per-user managed installation under `~/.edgedisco`, using LaunchAgents in the user's GUI domain. This roadmap defines the architecture, packaging, security boundaries, and implementation phases for an enterprise macOS version that can be deployed by administrators as root via MDM (Jamf Pro, Kandji, Mosyle, Microsoft Intune), run background components with appropriate privilege separation, and present a native, lightweight menu bar status application.

---

## 1. Architectural principles & privilege separation

Enterprise macOS software must respect Apple's modern security architecture: Transparency, Consent, and Control (TCC), System Integrity Protection (SIP), Gatekeeper, Hardened Runtime, and user session isolation.

A monolithic root daemon cannot cleanly handle local AI runtime inspection because developer tools (Cursor, Claude Code, GitHub Copilot) run inside the user's login session and write configuration and hooks inside the user's home directory. Furthermore, starting in macOS 12+, root is subject to TCC protections and cannot read protected user directories without explicit enterprise authorization.

EdgeDisco Enterprise uses a **three-tier architecture**:

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
└─────────────────────────────────────────────────────────────┘
```

### Component roles

| Tier | Lifecycle | Execution Context | Responsibility |
| --- | --- | --- | --- |
| **Menu Bar App** | Login session | Logged-in user | Status visualization, menu actions, browser dashboard bootstrapping |
| **Per-User Agent** | LaunchAgent (`gui/<uid>`) | Logged-in user | Runtime hooks, user config detection, local event spooling |
| **System Daemon** | LaunchDaemon (`system`) | Root or `_edgedisco` | Central database, OTLP telemetry delivery, fleet server sync, host process inventory |

---

## 2. Native menu bar application (Swift / AppKit)

The user interface follows the lightweight model popularized by tools like Tailscale and Osquery:

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

## 3. Packaging & deployment (.pkg vs .app)

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
  - `data/`: Central SQLite database (`inventory.db`) with `0700` permissions owned by the system daemon.
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
   - On macOS 13+, leverages `SMAppService` for programmatic LaunchAgent/LaunchDaemon lifecycle management where available.

---

## 4. Code signing, notarization, and TCC

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

## 5. MDM configuration & fleet policy

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

## 6. Implementation roadmap

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

### Phase 3: MDM managed preferences & enterprise validation
- Implement Managed Preferences parser reading `/Library/Managed Preferences/com.edgedisco.agent.plist`.
- Ship a downloadable reference `.mobileconfig` profile for MDM distribution.
- Test deployment across Jamf Pro and Microsoft Intune environments on clean macOS 13, 14, and 15 machines.
