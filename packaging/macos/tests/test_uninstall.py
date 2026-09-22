from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
UNINSTALL = REPO / "packaging" / "macos" / "uninstall.sh"
BINARY = Path("usr/local/libexec/edgedisco/edgedisco")
DAEMON_PLIST = Path("Library/LaunchDaemons/com.edgedisco.daemon.plist")
AGENT_PLIST = Path("Library/LaunchAgents/com.edgedisco.agent.plist")
SOCKET = Path("var/run/edgedisco.sock")
SUPPORT = Path("Library/Application Support/EdgeDisco")


def run_uninstall(
    root: Path | None,
    *args: str,
    lifecycle_log: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("EDGEDISCO_STAGED_ROOT", None)
    env.pop("EDGEDISCO_LIFECYCLE_LOG", None)
    if root is not None:
        env["EDGEDISCO_STAGED_ROOT"] = str(root)
    if lifecycle_log is not None:
        env["EDGEDISCO_LIFECYCLE_LOG"] = str(lifecycle_log)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(UNINSTALL), *args],
        cwd=REPO,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def seed_install(root: Path) -> tuple[Path, Path]:
    for relative in (BINARY, DAEMON_PLIST, AGENT_PLIST, SOCKET):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fixture:{relative}".encode())
    data = root / SUPPORT / "data"
    data.mkdir(parents=True)
    inventory = data / "inventory.db"
    wal = data / "inventory.db-wal"
    inventory.write_bytes(b"inventory-state\x00\xff")
    wal.write_bytes(b"wal-state\x00\xfe")
    (root / SUPPORT / "config/settings.toml").parent.mkdir(parents=True)
    (root / SUPPORT / "config/settings.toml").write_text("enabled = true\n")
    (root / SUPPORT / "logs/daemon.log").parent.mkdir(parents=True)
    (root / SUPPORT / "logs/daemon.log").write_text("log fixture\n")
    return inventory, wal


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_default_removes_package_files_and_preserves_runtime_data(tmp_path: Path):
    root = tmp_path / "staged root"
    root.mkdir()
    inventory, wal = seed_install(root)
    before = (digest(inventory), digest(wal))
    lifecycle = tmp_path / "lifecycle.log"

    result = run_uninstall(root, lifecycle_log=lifecycle)

    assert result.returncode == 0, result.stderr
    for relative in (BINARY, DAEMON_PLIST, AGENT_PLIST, SOCKET):
        assert not (root / relative).exists()
    assert (digest(inventory), digest(wal)) == before
    assert (root / SUPPORT / "config/settings.toml").is_file()
    assert (root / SUPPORT / "logs/daemon.log").is_file()
    assert lifecycle.read_text().splitlines() == [
        "launchctl bootout system/com.edgedisco.daemon",
        "launchctl bootout gui/<console-uid>/com.edgedisco.agent",
        "pkgutil --forget com.edgedisco.pkg",
    ]


def test_purge_removes_entire_application_support_tree(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    seed_install(root)

    result = run_uninstall(root, "--purge")

    assert result.returncode == 0, result.stderr
    assert not (root / SUPPORT).exists()


def test_help_and_unknown_flag_are_bounded_and_non_mutating(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    inventory, wal = seed_install(root)
    before = (digest(inventory), digest(wal))

    help_result = run_uninstall(None, "--help")
    unknown = run_uninstall(root, "--definitely-unknown")

    assert help_result.returncode == 0
    assert "usage:" in help_result.stdout.lower()
    assert unknown.returncode != 0
    assert "usage:" in unknown.stderr.lower()
    assert (digest(inventory), digest(wal)) == before
    for relative in (BINARY, DAEMON_PLIST, AGENT_PLIST, SOCKET):
        assert (root / relative).exists()


def test_production_mode_refuses_non_root_before_mutation(tmp_path: Path):
    assert os.geteuid() != 0, "this native macOS test must run without root"
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"must survive")
    before = digest(sentinel)

    result = run_uninstall(None)

    assert result.returncode == 77
    assert "requires root" in result.stderr.lower()
    assert digest(sentinel) == before


def test_staged_missing_services_log_intent_without_running_lifecycle_tools(
    tmp_path: Path,
):
    root = tmp_path / "empty root"
    root.mkdir()
    lifecycle = tmp_path / "lifecycle.log"
    marker = tmp_path / "external-command-ran"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for command in ("launchctl", "pkgutil"):
        executable = fake_bin / command
        executable.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\nexit 99\n")
        executable.chmod(0o755)

    result = run_uninstall(
        root,
        lifecycle_log=lifecycle,
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert lifecycle.read_text().splitlines() == [
        "launchctl bootout system/com.edgedisco.daemon",
        "launchctl bootout gui/<console-uid>/com.edgedisco.agent",
        "pkgutil --forget com.edgedisco.pkg",
    ]


def test_symlink_in_package_owned_path_is_refused_before_any_mutation(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    for relative in (BINARY, DAEMON_PLIST, AGENT_PLIST, SOCKET):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"must remain")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "inventory.db"
    protected.write_bytes(b"outside-state")
    support_parent = root / SUPPORT.parent
    support_parent.mkdir(parents=True, exist_ok=True)
    (root / SUPPORT).symlink_to(outside, target_is_directory=True)
    before = digest(protected)

    result = run_uninstall(root, "--purge")

    assert result.returncode == 65
    assert "refusing symlink" in result.stderr.lower()
    assert digest(protected) == before
    for relative in (BINARY, DAEMON_PLIST, AGENT_PLIST, SOCKET):
        assert (root / relative).read_bytes() == b"must remain"


def test_staged_root_must_be_absolute_canonical_directory(tmp_path: Path):
    relative = run_uninstall(Path("relative-root"))
    assert relative.returncode == 64
    assert "absolute" in relative.stderr.lower()

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    linked = run_uninstall(linked_root)
    assert linked.returncode == 64
    assert "canonical" in linked.stderr.lower() or "symlink" in linked.stderr.lower()
