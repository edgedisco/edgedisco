# Native macOS enterprise package

This document covers the native enterprise package only. It is separate from the legacy per-user Python installation in `deployment-macos.md`. The endpoint package contains the Rust executable, the native Swift menu-bar application, two launchd property lists, and installer scripts. It contains no Python runtime, web server, dashboard assets, MCP server, credentials, enrollment token, signing certificate, or Notary profile.

## Installed layout and ownership

The flat package identifier is `com.edgedisco.pkg`. `pkgbuild --ownership recommended` records every payload object as `root:wheel`; `postinstall` enforces the modes again without recursively changing existing runtime state.

| Path | Owner | Mode | Ownership boundary |
| --- | --- | ---: | --- |
| `/usr/local/libexec/edgedisco` | `root:wheel` | `0755` | package executable directory |
| `/usr/local/libexec/edgedisco/edgedisco` | `root:wheel` | `0755` | package |
| `/Applications/EdgeDisco.app` and its directories | `root:wheel` | `0755` | package application bundle |
| `/Applications/EdgeDisco.app/Contents/MacOS/EdgeDiscoMenuBar` | `root:wheel` | `0755` | package |
| `/Applications/EdgeDisco.app/Contents/Info.plist` | `root:wheel` | `0644` | package |
| `/Library/LaunchDaemons/com.edgedisco.daemon.plist` | `root:wheel` | `0644` | package |
| `/Library/LaunchAgents/com.edgedisco.agent.plist` | `root:wheel` | `0644` | package |
| `/Library/Application Support/EdgeDisco` | `root:wheel` | `0750` | package directory |
| `.../config` and `.../logs` | `root:wheel` | `0750` | package directories |
| `.../data` | `root:wheel` | `0700` | runtime directory; contents are preserved |

The LaunchDaemon label is `com.edgedisco.daemon`. It runs the native binary in system IPC mode, uses `/Library/Application Support/EdgeDisco/data/inventory.db`, and binds `/var/run/edgedisco.sock`. The current package authorizes only UID 0 on that socket. Expanding access requires an explicit installation policy in a later release; the per-user collector is not granted system database or socket access implicitly.

The LaunchAgent label is `com.edgedisco.agent`. It runs `edgedisco scan` every 60 seconds in the login session. Its working directory is `/`, which is traversable by ordinary users and does not grant access to enterprise state. With no `--db` or IPC arguments, the CLI resolves the user's own `~/.edgedisco/data/inventory.db`; it cannot claim the enterprise database or system socket. It is a finite collector job, so `StartInterval` rather than `KeepAlive` controls its lifecycle.

## Build an unsigned package

Requirements: macOS, Rust/Cargo, `plutil`, `pkgbuild`, `productbuild`, `pkgutil`, `lsbom`, and `codesign` inspection tooling. The build is local and does not use the network, install files, invoke `sudo`, or mutate launchd.

From a clean checkout:

```sh
make macos-pkg VERSION=0.1.0
# equivalent public entry point:
./packaging/macos/build-pkg.sh 0.1.0
```

The command compiles `target/release/edgedisco`, runs `swift build -c release` for `macos/EdgeDiscoMenuBar`, assembles `/Applications/EdgeDisco.app`, and writes:

```text
dist/EdgeDisco-0.1.0-unsigned.pkg
```

For a previously built binary, use `--binary /absolute/path/to/edgedisco`. `--output-dir` selects another artifact directory. Versions are validated before the output path is created.

The application bundle has this payload layout:

```text
/Applications/EdgeDisco.app/
└── Contents/
    ├── Info.plist
    ├── MacOS/
    │   └── EdgeDiscoMenuBar
    └── Resources/
```

`Info.plist` declares `com.edgedisco.menubar`, uses the package version for both bundle-version keys, and sets `LSUIElement` to `true` so the process runs as a menu-bar application without a Dock icon.

## Verify and inspect

Run the behavioral packaging suite, plist lint, and artifact inspection:

```sh
python3 -m pytest -q packaging/macos/tests/test_packaging.py
plutil -lint packaging/macos/launchd/com.edgedisco.daemon.plist
plutil -lint packaging/macos/launchd/com.edgedisco.agent.plist
pkgutil --check-signature dist/EdgeDisco-0.1.0-unsigned.pkg
mkdir -p dist/expanded
pkgutil --expand-full dist/EdgeDisco-0.1.0-unsigned.pkg dist/expanded
pkgutil --payload-files dist/EdgeDisco-0.1.0-unsigned.pkg
lsbom -p 'fm?' dist/expanded/EdgeDisco.pkg/Bom
codesign -dvv dist/expanded/EdgeDisco.pkg/Payload/usr/local/libexec/edgedisco/edgedisco
plutil -lint dist/expanded/EdgeDisco.pkg/Payload/Applications/EdgeDisco.app/Contents/Info.plist
shasum -a 256 dist/EdgeDisco-0.1.0-unsigned.pkg
```

For the local artifact, `pkgutil --check-signature` must report `Status: no signature`. That is expected and must never be represented as release-ready. `pkgutil --expand-full` should expose only the native binary, `EdgeDisco.app`, both launchd assets, the declared empty runtime directories, and `preinstall`/`postinstall`. macOS filesystem provenance may appear as `._` metadata records in the BOM; these are not executable package files.

## Uninstall

Run the repository's bounded uninstaller as root on an installed endpoint:

```sh
sudo ./packaging/macos/uninstall.sh
```

The default uninstall unloads only the EdgeDisco launchd labels, removes `/Applications/EdgeDisco.app`, the package-owned Rust binary and plists, removes the runtime socket, and forgets the package receipt. It preserves `/Library/Application Support/EdgeDisco`, including configuration, logs, inventory, and outbox state. To remove that retained application-support tree as well, use the explicit destructive option:

```sh
sudo ./packaging/macos/uninstall.sh --purge
```

For non-root verification, set `EDGEDISCO_STAGED_ROOT` to an existing absolute canonical staging directory. Staged mode removes the staged app bundle and other package files without invoking `launchctl` or `pkgutil`.

The staged-root test runs both lifecycle scripts twice against a temporary path containing spaces. It verifies modes, records `root:wheel` ownership intent without requiring root, and compares hashes of existing `inventory.db` and `outbox.db` before and after both passes. Production package scripts reject non-root execution; staged mode rejects relative paths, `/`, non-canonical roots, and symlink escapes, and never invokes launchctl.

## Upgrade lifecycle

`preinstall` issues only label-scoped launchctl operations for `com.edgedisco.agent` and `com.edgedisco.daemon`. Missing services are harmless. It does not use `killall`, `pkill`, wildcard cleanup, or remove data.

`postinstall`:

1. validates that the binary and both plists exist;
2. creates only the declared application-support directories;
3. enforces file and directory modes and ownership without recursively modifying data files;
4. bootstraps the system daemon; and
5. when a GUI console session exists, bootstraps the user agent.

Reinstalling the same or a newer package preserves `/Library/Application Support/EdgeDisco/data`, including inventory and outbox rows.

## Roll back an upgrade

Keep the previous package in a protected administrator-controlled location. The following procedure stops only EdgeDisco, records the state hash, installs the previous package, and confirms that state survived. The previous package's `postinstall` bootstraps the daemon before the current console agent, which is the dependency order.

```sh
PREVIOUS_PKG=/path/to/EdgeDisco-0.1.0.pkg
DATA='/Library/Application Support/EdgeDisco/data'
CONSOLE_UID=$(stat -f '%u' /dev/console)
sudo launchctl bootout "gui/$CONSOLE_UID/com.edgedisco.agent" 2>/dev/null || true
sudo launchctl bootout system/com.edgedisco.daemon 2>/dev/null || true
sudo shasum -a 256 "$DATA/inventory.db" > /tmp/edgedisco-inventory.before
sudo installer -pkg "$PREVIOUS_PKG" -target /
sudo shasum -a 256 -c /tmp/edgedisco-inventory.before
sudo launchctl print system/com.edgedisco.daemon
launchctl print "gui/$CONSOLE_UID/com.edgedisco.agent"
```

Do not delete or replace `data` during rollback. If there is no logged-in GUI user, the LaunchAgent loads at the next login; only the daemon is expected immediately.

## Future signing and notarization

Signing is intentionally separate from unsigned local builds. No identity or profile is stored in this repository. All three inputs are mandatory, including for a non-mutating plan:

```sh
export DEVELOPER_ID_APPLICATION='Developer ID Application: Legal Name (TEAMID)'
export DEVELOPER_ID_INSTALLER='Developer ID Installer: Legal Name (TEAMID)'
export NOTARY_PROFILE='edgedisco-notary'
./packaging/macos/release.sh --plan dist/EdgeDisco-0.1.0-unsigned.pkg
```

Without any one input, the script exits before expansion, signing, output mutation, Keychain access, or submission. With all inputs, `--plan` expands into a disposable temporary directory and validates the package identifier/version, exact complete object manifest, absence of symlinks or special objects, Mach-O executable type, every package-owned mode and BOM owner, canonical plists, and canonical lifecycle scripts. It then prints the codesign, package signing, Notary submission, stapling, and atomic publication sequence without running those release actions.

A future credentialed release operator may omit `--plan`. The script then verifies local identities, signs the validated Rust executable with Hardened Runtime and timestamping, rebuilds the component, and writes the signed product to a hidden candidate beside the final output path. It submits that candidate with `xcrun notarytool --keychain-profile`, staples the ticket, and checks the package signature. Only after every check passes does it atomically rename the same-filesystem candidate to `EdgeDisco-<version>.pkg`; the cleanup trap removes a failed candidate, so signing, notarization, or stapling failure cannot leave a newly published release-named artifact. This path requires network access and valid Apple credentials and is not executed by local tests.

## Requirement-to-evidence map

| ID | Requirement | Evidence |
| --- | --- | --- |
| MACPKG-001 | Exact daemon/agent separation and plist validity | `test_launchd_plists_are_valid_and_separate_privileges`; `plutil -lint` |
| MACPKG-002 | Idempotent, state-preserving lifecycle | `test_staged_scripts_are_repeatable_preserve_state_and_record_ownership` |
| MACPKG-003 | Real unsigned flat package, application bundle, and exact payload | `test_real_flat_package_payload_scripts_modes_and_unsigned_signature`; `plutil`, `pkgutil`, `lsbom` |
| MACPKG-004 | Fail-closed release preflight | `test_release_preflight_fails_closed_and_plan_is_non_mutating` |
| MACPKG-005 | Invalid inputs cannot create artifacts or target the host | malformed-version and staged-root negative tests |
| MACPKG-006 | Bounded uninstall removes the application bundle and preserves data by default | `test_default_removes_package_files_and_preserves_runtime_data` |
