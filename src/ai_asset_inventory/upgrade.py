"""Installer snapshots and rollback, executed from the downloaded source tree."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .configuration import load_config
from .database import DATABASE_VERSION
from .self_service import AGENT_LABEL, SERVER_LABEL, _health, _launchctl, _parse_env, _wait_for_server


def targets(root: Path, home: Path) -> dict[str, Path]:
    result = {name: root / name for name in (
        "agent.json", "server.env", "venv", "bin", "cli-launcher.path", "data/inventory.db",
    )}
    for relative in (".cursor/hooks.json", ".claude/settings.json", ".copilot/hooks/edgedisco.json",
                     ".local/bin/edgedisco", ".zprofile", ".bash_profile", ".bash_login", ".profile"):
        result["home/" + relative] = home / relative
    for label in (SERVER_LABEL, AGENT_LABEL):
        relative = f"Library/LaunchAgents/{label}.plist"
        result["home/" + relative] = home / relative
    return result


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.name == "inventory.db":
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(destination) as saved:
                original.backup(saved)
        original.close()
        saved.close()
        destination.chmod(0o600)
    else:
        shutil.copy2(source, destination)


def snapshot(root: Path, home: Path) -> Path:
    root, home = root.resolve(), home.resolve()
    if root == home or root in home.parents or root in {Path("/tmp"), Path("/private/tmp")}:
        raise RuntimeError("Installation root must be a dedicated EdgeDisco directory")
    load_config(root / "agent.json")  # Fail before modifying any installation files.
    db = root / "data/inventory.db"
    if db.exists():
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        if version > DATABASE_VERSION:
            raise RuntimeError("Database requires a newer EdgeDisco release; refusing downgrade")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = root / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    present = []
    for key, path in targets(root, home).items():
        if path.exists() or path.is_symlink():
            _copy(path, backup / "before" / key)
            present.append(key)
    manifest = {"root": str(root), "home": str(home), "present": present}
    descriptor = os.open(backup / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(manifest, stream)
    return backup


def stop_services(root: Path) -> None:
    domain = f"gui/{os.getuid()}"
    for label in (AGENT_LABEL, SERVER_LABEL):
        _launchctl("bootout", f"{domain}/{label}", check=False)
    if not (root / "server.env").exists():
        return
    port = int(_parse_env(root / "server.env").get("EDGEDISCO_PORT", "8080"))
    deadline = time.monotonic() + 5
    while _health(port) is not None:
        if time.monotonic() >= deadline:
            raise RuntimeError("A stale server is still running after stop; refusing to replace installation files")
        time.sleep(0.1)


def start_services(home: Path) -> None:
    domain = f"gui/{os.getuid()}"
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(key, None)
    for label in (SERVER_LABEL, AGENT_LABEL):
        plist = home / "Library/LaunchAgents" / f"{label}.plist"
        if plist.exists():
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                           env=environment, capture_output=True, text=True, check=True)


def restore(backup: Path) -> None:
    backup = backup.resolve()
    manifest = json.loads((backup / "manifest.json").read_text())
    root, home = Path(manifest["root"]), Path(manifest["home"])
    if root == home or root in home.parents or root in {Path("/tmp"), Path("/private/tmp")}:
        raise RuntimeError("Unsafe installation root in backup")
    if backup.parent != root / "backups":
        raise RuntimeError("Backup does not belong to the installation")
    paths = targets(root, home)
    if set(manifest["present"]) - set(paths):
        raise RuntimeError("Invalid backup manifest")
    stop_services(root)
    # Keep failed-attempt evidence/configuration for inspection or recovery.
    # Spool files are deliberately untouched: hooks may have appended events.
    failed = backup / "failed-state"
    if failed.exists():
        failed = backup / f"failed-state-{time.time_ns()}"
    failed.mkdir(mode=0o700)
    for key, target in paths.items():
        if target.exists() or target.is_symlink():
            destination = failed / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(destination))
        if key == "data/inventory.db":
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(target) + suffix)
                if sidecar.exists():
                    destination = failed / "data" / sidecar.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sidecar), str(destination))
        if key in manifest["present"]:
            _copy(backup / "before" / key, target)
    start_services(home)
    if (home / "Library/LaunchAgents" / f"{SERVER_LABEL}.plist").exists():
        values = _parse_env(root / "server.env")
        _wait_for_server(int(values.get("EDGEDISCO_PORT", "8080")), values["AAI_ADMIN_TOKEN"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "stop", "restore"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.action == "snapshot":
        print(snapshot(args.root, Path.home()))
    elif args.action == "stop":
        stop_services(args.root)
    else:
        restore(args.backup)


if __name__ == "__main__":
    main()
