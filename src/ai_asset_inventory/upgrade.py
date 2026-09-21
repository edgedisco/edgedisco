"""Installer snapshots and rollback, executed from the downloaded source tree."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .configuration import load_config
from .database import DATABASE_VERSION
from .self_service import (AGENT_LABEL, SERVER_LABEL, _health, _launchctl, _parse_env,
                           _systemctl, _wait_for_server)


def _database_version(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(
            f"Cannot read existing EdgeDisco database at {path}: expected a regular file. "
            "Check its type, ownership, and permissions before reinstalling."
        )
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(
            f"Cannot open existing EdgeDisco database at {path}. "
            "Check ownership and permissions on the file and its parent directories; "
            "the installer will not replace an unreadable database."
        ) from exc


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
        relative = f".config/systemd/user/{label}.service"
        result["home/" + relative] = home / relative
    return result


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    elif source.name == "inventory.db":
        try:
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
                with sqlite3.connect(destination) as saved:
                    original.backup(saved)
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(
                f"Cannot back up existing EdgeDisco database at {source}. "
                "Check ownership, permissions, and available disk space; "
                "the installer has left the existing database in place."
            ) from exc
        destination.chmod(0o600)
    else:
        shutil.copy2(source, destination)


def snapshot(root: Path, home: Path, *, quiesce: bool = False) -> Path:
    root, home = root.resolve(), home.resolve()
    if root == home or root in home.parents or root in {Path("/tmp"), Path("/private/tmp")}:
        raise RuntimeError("Installation root must be a dedicated EdgeDisco directory")
    load_config(root / "agent.json")  # Fail before modifying any installation files.
    db = root / "data/inventory.db"
    if db.exists():
        version = _database_version(db)
        if version > DATABASE_VERSION:
            raise RuntimeError("Database requires a newer EdgeDisco release; refusing downgrade")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = root / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    present = []
    try:
        if quiesce:
            stop_services(root)
        for key, path in targets(root, home).items():
            if path.exists() or path.is_symlink():
                _copy(path, backup / "before" / key)
                present.append(key)
        manifest = {"root": str(root), "home": str(home), "present": present}
        descriptor = os.open(backup / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(manifest, stream)
    except Exception:
        if quiesce:
            start_services(home)
        raise
    return backup


def stop_services(root: Path) -> None:
    if platform.system() == "Linux":
        for label in (AGENT_LABEL, SERVER_LABEL):
            _systemctl("stop", f"{label}.service", check=False)
    else:
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
    if platform.system() == "Linux":
        _systemctl("daemon-reload")
        for label in (SERVER_LABEL, AGENT_LABEL):
            unit = home / ".config/systemd/user" / f"{label}.service"
            if unit.exists():
                _systemctl("enable", "--now", f"{label}.service")
        return
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
    # Setup verification may have accepted reports and drained their spool.
    # Project that evidence into the old schema before restarting old code.
    failed_db = failed / "data/inventory.db"
    restored_db = root / "data/inventory.db"
    if failed_db.exists() and restored_db.exists():
        _preserve_evidence(failed_db, restored_db)
    start_services(home)
    server_definition = ((home / "Library/LaunchAgents" / f"{SERVER_LABEL}.plist").exists() or
                         (home / ".config/systemd/user" / f"{SERVER_LABEL}.service").exists())
    if server_definition:
        values = _parse_env(root / "server.env")
        _wait_for_server(int(values.get("EDGEDISCO_PORT", "8080")), values["AAI_ADMIN_TOKEN"])


def _preserve_evidence(source: Path, destination: Path) -> None:
    """Retain accepted evidence using columns supported by the rollback schema.

    Never copy schema/version or credentials over restored endpoint config.
    The full newer database remains in failed-state for fields absent in the
    old schema. Repeating rollback remains idempotent through primary keys.
    """
    with closing(sqlite3.connect(destination)) as conn, conn:
        conn.execute("ATTACH DATABASE ? AS accepted", (str(source),))
        conn.execute("BEGIN IMMEDIATE")
        for table in ("devices", "scans", "assets", "runtime_events", "agent_sessions",
                      "otlp_outbox", "otlp_asset_state"):
            old = conn.execute(f"PRAGMA main.table_info({table})").fetchall()
            new = conn.execute(f"PRAGMA accepted.table_info({table})").fetchall()
            if not old or not new:
                continue
            names = {row[1] for row in new}
            columns = [row[1] for row in old if row[1] in names]
            fields = ','.join('"' + col + '"' for col in columns)
            if table in {"assets", "agent_sessions", "otlp_outbox", "otlp_asset_state"}:
                keys = [row[1] for row in old if row[5]]
                updates = ','.join(f'"{col}"=excluded."{col}"' for col in columns if col not in keys)
                conflict = ','.join('"' + col + '"' for col in keys)
                sql = (f'INSERT INTO main.{table} ({fields}) SELECT {fields} FROM accepted.{table} WHERE true '
                       f'ON CONFLICT ({conflict}) DO UPDATE SET {updates}')
                if table != "otlp_outbox":
                    clock = "updated_at" if table == "otlp_asset_state" else "last_seen"
                    sql += f' WHERE excluded.{clock} >= {table}.{clock}'
            else:
                sql = f'INSERT OR IGNORE INTO main.{table} ({fields}) SELECT {fields} FROM accepted.{table}'
            conn.execute(sql)
        if conn.execute("PRAGMA main.foreign_key_check").fetchone():
            raise RuntimeError("Cannot reconcile accepted evidence into rollback database")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "stop", "restore"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "snapshot":
            print(snapshot(args.root, Path.home(), quiesce=True))
        elif args.action == "stop":
            stop_services(args.root)
        else:
            restore(args.backup)
    except RuntimeError as exc:
        parser.exit(1, f"EdgeDisco upgrade error: {exc}\n")


if __name__ == "__main__":
    main()
