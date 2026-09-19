from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, hostname TEXT NOT NULL, os TEXT NOT NULL,
                    os_version TEXT, machine TEXT, agent_version TEXT,
                    token_hash TEXT UNIQUE NOT NULL, enrolled_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scans (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
                    observed_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    asset_count INTEGER NOT NULL, privacy_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    device_id TEXT NOT NULL REFERENCES devices(id), fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL, name TEXT NOT NULL, vendor TEXT NOT NULL,
                    version TEXT, path_hash TEXT, command_hash TEXT, metadata_json TEXT NOT NULL,
                    running INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(device_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name);
                CREATE INDEX IF NOT EXISTS idx_assets_seen ON assets(last_seen);
            """)

    def enroll(self, metadata: dict[str, Any]) -> tuple[str, str]:
        device_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (device_id, str(metadata.get("hostname", "unknown"))[:255],
                 str(metadata.get("os", "unknown"))[:64], str(metadata.get("os_version", ""))[:255],
                 str(metadata.get("machine", ""))[:64], str(metadata.get("agent_version", ""))[:32],
                 token_hash(token), now, now),
            )
        return device_id, token

    def device_for_token(self, token: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM devices WHERE token_hash=?", (token_hash(token),)).fetchone()

    def ingest(self, device_id: str, report: dict[str, Any]) -> int:
        observed_at = str(report["observed_at"])
        received_at = utc_now()
        scan_id = str(report["scan_id"])
        assets = report.get("assets", [])
        with self.connect() as conn:
            existing = conn.execute("SELECT asset_count FROM scans WHERE id=?", (scan_id,)).fetchone()
            if existing:
                return int(existing["asset_count"])
            conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (received_at, device_id))
            conn.execute(
                "INSERT INTO scans VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, device_id, observed_at, received_at, len(assets),
                 json.dumps(report.get("privacy", {}), sort_keys=True)),
            )
            conn.execute("UPDATE assets SET running=0 WHERE device_id=?", (device_id,))
            for item in assets:
                conn.execute("""
                    INSERT INTO assets(device_id,fingerprint,kind,name,vendor,version,path_hash,
                      command_hash,metadata_json,running,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(device_id,fingerprint) DO UPDATE SET
                      kind=excluded.kind,name=excluded.name,vendor=excluded.vendor,
                      version=excluded.version,path_hash=excluded.path_hash,
                      command_hash=excluded.command_hash,metadata_json=excluded.metadata_json,
                      running=excluded.running,last_seen=excluded.last_seen
                """, (device_id, str(item.get("fingerprint", ""))[:128],
                      str(item.get("kind", "unknown"))[:64], str(item.get("name", "unknown"))[:255],
                      str(item.get("vendor", "Unknown"))[:255], item.get("version"),
                      item.get("path_hash"), item.get("command_hash"),
                      json.dumps(item.get("metadata", {}), sort_keys=True),
                      1 if item.get("running") else 0, observed_at, observed_at))
        return len(assets)

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            running = conn.execute("SELECT COUNT(*) FROM assets WHERE running=1").fetchone()[0]
            mcp = conn.execute("SELECT COUNT(*) FROM assets WHERE kind='mcp_server'").fetchone()[0]
            agents = conn.execute("SELECT COUNT(*) FROM assets WHERE kind='agent_runtime' AND running=1").fetchone()[0]
            recent = [dict(row) for row in conn.execute("""
                SELECT a.name,a.vendor,a.kind,a.running,a.first_seen,a.last_seen,
                       d.hostname,d.os,d.id AS device_id,a.metadata_json
                FROM assets a JOIN devices d ON d.id=a.device_id
                ORDER BY a.last_seen DESC LIMIT 500
            """)]
        for row in recent:
            row["metadata"] = json.loads(row.pop("metadata_json"))
            row["running"] = bool(row["running"])
        return {"devices": devices, "assets": assets, "running": running, "active_agents": agents, "mcp_servers": mcp, "items": recent}

    def export_csv(self) -> str:
        data = self.summary()["items"]
        output = io.StringIO()
        fields = ["device_id", "hostname", "os", "kind", "name", "vendor", "running", "first_seen", "last_seen"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in data:
            writer.writerow({key: item.get(key) for key in fields})
        return output.getvalue()
