# Deploy on Linux

EdgeDisco self-service installation supports Linux hosts that meet all of these conditions:

- A distribution using systemd, such as current Ubuntu, Debian, Fedora, RHEL, Rocky Linux, AlmaLinux, openSUSE, or Arch Linux.
- A logged-in, non-root user with an active `systemd --user` manager and user D-Bus session.
- Python 3.9 or newer with the standard `venv` module.
- Ordinary permission to inspect that user's processes.
- An internet connection for remote installation, unless running `install.sh` from a local checkout.

EdgeDisco does **not** claim self-service support for containers, WSL instances without systemd, non-systemd distributions, SSH sessions without a systemd user manager, headless service accounts without a user session, or root/system services. Those environments can use the manual Python or container deployment described in the README.

## Install or upgrade

Download and inspect the installer, then run it as the target desktop user:

```bash
curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh
less install.sh
bash install.sh
```

The same flags and environment overrides apply on macOS and Linux:

```bash
bash install.sh --help
bash install.sh --yes --no-open
bash install.sh --port 8090 --all-adapters
EDGEDISCO_HOME="$HOME/.edgedisco-test" bash install.sh --yes --no-open
```

The installer creates a managed virtual environment, credentials, configuration, database, CLI launcher, and two services under `~/.config/systemd/user/`:

- `com.edgedisco.server.service`
- `com.edgedisco.agent.service`

It runs `systemctl --user`; it never invokes `sudo`, creates a system service, or scans another user's processes. Reinstallation snapshots the managed state, preserves enrollment and custom configuration, and attempts automatic rollback on failure.

Running `bash ./install.sh` from a source checkout installs that checkout directly and prints its path, which allows testing uncommitted changes. A standalone or stdin installer downloads the configured source archive. Reinstallation replaces the managed package even when the version number is unchanged, then performs and uploads a fresh complete inventory before restarting the agent. An unreadable, symlinked, or non-regular existing database stops the upgrade with ownership and permission guidance; it is never silently replaced.

## Verify

Open a new shell after installation:

```bash
edgedisco status
edgedisco dashboard
systemctl --user status com.edgedisco.server.service
systemctl --user status com.edgedisco.agent.service
```

`edgedisco dashboard` creates a single-use authenticated local browser session without printing the administrator token. `edgedisco demo` optionally adds clearly labeled synthetic evidence.

Linux discovery includes the current user's process table, bounded CLI locations, `.desktop` entries under `/usr/share/applications` and `~/.local/share/applications`, and supported MCP configuration paths. It does not recursively crawl the filesystem or require audit, eBPF, `ptrace`, or elevated permissions.

## Service lifecycle and logs

```bash
systemctl --user restart com.edgedisco.server.service com.edgedisco.agent.service
journalctl --user -u com.edgedisco.server.service -u com.edgedisco.agent.service
edgedisco uninstall
```

Uninstall removes only the EdgeDisco user units, managed CLI launcher, and managed application hooks. Configuration, credentials, logs, and evidence remain unless `edgedisco uninstall --purge --yes` is used.

Some distributions stop user services after logout unless user lingering is enabled. EdgeDisco does not enable lingering because doing so is an administrator policy decision. Without lingering, services start with the user's systemd session and stop when that session ends; this is appropriate for current-user inventory.

## Unsupported systemd state

If `systemctl --user show-environment` fails, the installer exits before creating or modifying the installation. Do not work around that error with `sudo`: a root service would inventory the wrong user. Use a graphical/login user session, configure the distribution's user manager, or follow the manual deployment instead.
