from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
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
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
                    observed_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    app TEXT NOT NULL, event_type TEXT NOT NULL,
                    session_hash TEXT NOT NULL, agent_hash TEXT NOT NULL,
                    agent_type TEXT, tool_name TEXT, mcp_server TEXT, model TEXT,
                    status TEXT NOT NULL, duration_ms INTEGER,
                    workspace_hash TEXT, user_hash TEXT, metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_seen ON runtime_events(observed_at);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_session ON runtime_events(device_id,session_hash,agent_hash);
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    device_id TEXT NOT NULL REFERENCES devices(id),
                    session_hash TEXT NOT NULL, agent_hash TEXT NOT NULL,
                    app TEXT NOT NULL, agent_type TEXT, model TEXT,
                    status TEXT NOT NULL, workspace_hash TEXT, user_hash TEXT,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    last_event TEXT NOT NULL, event_count INTEGER NOT NULL,
                    tool_count INTEGER NOT NULL, mcp_count INTEGER NOT NULL,
                    duration_ms INTEGER,
                    PRIMARY KEY(device_id,session_hash,agent_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_seen ON agent_sessions(last_seen);
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
        active_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        with self.connect() as conn:
            devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            running = conn.execute("SELECT COUNT(*) FROM assets WHERE running=1").fetchone()[0]
            mcp = conn.execute("SELECT COUNT(*) FROM assets WHERE kind='mcp_server'").fetchone()[0]
            agents = conn.execute("SELECT COUNT(*) FROM assets WHERE kind='agent_runtime' AND running=1").fetchone()[0]
            active_sessions = conn.execute(
                "SELECT COUNT(*) FROM agent_sessions WHERE status='active' AND last_seen>=?",
                (active_cutoff,),
            ).fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
            recent = [dict(row) for row in conn.execute("""
                SELECT a.name,a.vendor,a.kind,a.running,a.first_seen,a.last_seen,
                       d.hostname,d.os,d.id AS device_id,a.metadata_json
                FROM assets a JOIN devices d ON d.id=a.device_id
                ORDER BY a.last_seen DESC LIMIT 500
            """)]
            session_items = [dict(row) for row in conn.execute("""
                SELECT s.device_id,s.session_hash,s.agent_hash,s.app,s.agent_type,s.model,
                       CASE WHEN s.status='active' AND s.last_seen<? THEN 'stale' ELSE s.status END AS status,
                       s.workspace_hash,s.user_hash,s.first_seen,s.last_seen,s.last_event,
                       s.event_count,s.tool_count,s.mcp_count,s.duration_ms,d.hostname,d.os
                FROM agent_sessions s
                JOIN devices d ON d.id=s.device_id
                ORDER BY s.last_seen DESC LIMIT 500
            """, (active_cutoff,))]
            event_items = [dict(row) for row in conn.execute("""
                SELECT e.id,e.observed_at,e.app,e.event_type,e.session_hash,e.agent_hash,
                       e.agent_type,e.tool_name,e.mcp_server,e.model,e.status,e.duration_ms,
                       e.metadata_json,d.hostname
                FROM runtime_events e JOIN devices d ON d.id=e.device_id
                ORDER BY e.observed_at DESC LIMIT 500
            """)]
        for row in recent:
            row["metadata"] = json.loads(row.pop("metadata_json"))
            row["running"] = bool(row["running"])
        for row in event_items:
            row["metadata"] = json.loads(row.pop("metadata_json"))
        return {
            "devices": devices, "assets": assets, "running": running,
            "active_agents": agents, "active_sessions": active_sessions,
            "agent_sessions": session_count, "mcp_servers": mcp,
            "items": recent, "session_items": session_items, "event_items": event_items,
        }

    @staticmethod
    def _session_status(event: dict[str, Any]) -> str:
        event_type = str(event["event_type"]).lower()
        if event_type in {"sessionend", "subagentstop", "taskcompleted"}:
            return "failed" if event.get("status") == "failed" else "completed"
        if event_type == "stop":
            return "idle"
        return "active"

    def ingest_runtime_events(self, device_id: str, events: list[dict[str, Any]]) -> int:
        received_at = utc_now()
        accepted = 0
        with self.connect() as conn:
            conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (received_at, device_id))
            for event in events:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO runtime_events(
                      id,device_id,observed_at,received_at,app,event_type,session_hash,
                      agent_hash,agent_type,tool_name,mcp_server,model,status,duration_ms,
                      workspace_hash,user_hash,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    event["event_id"], device_id, event["observed_at"], received_at,
                    event["app"], event["event_type"], event["session_hash"],
                    event["agent_hash"], event.get("agent_type"), event.get("tool_name"),
                    event.get("mcp_server"), event.get("model"), event["status"],
                    event.get("duration_ms"), event.get("workspace_hash"), event.get("user_hash"),
                    json.dumps(event.get("metadata", {}), sort_keys=True),
                ))
                if cursor.rowcount == 0:
                    continue
                accepted += 1
                conn.execute("""
                    INSERT INTO agent_sessions(
                      device_id,session_hash,agent_hash,app,agent_type,model,status,
                      workspace_hash,user_hash,first_seen,last_seen,last_event,
                      event_count,tool_count,mcp_count,duration_ms)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(device_id,session_hash,agent_hash) DO UPDATE SET
                      app=excluded.app,
                      agent_type=COALESCE(excluded.agent_type,agent_sessions.agent_type),
                      model=COALESCE(excluded.model,agent_sessions.model),
                      status=excluded.status,
                      workspace_hash=COALESCE(excluded.workspace_hash,agent_sessions.workspace_hash),
                      user_hash=COALESCE(excluded.user_hash,agent_sessions.user_hash),
                      last_seen=excluded.last_seen,last_event=excluded.last_event,
                      duration_ms=COALESCE(excluded.duration_ms,agent_sessions.duration_ms),
                      event_count=agent_sessions.event_count+1,
                      tool_count=agent_sessions.tool_count+excluded.tool_count,
                      mcp_count=agent_sessions.mcp_count+excluded.mcp_count
                """, (
                    device_id, event["session_hash"], event["agent_hash"], event["app"],
                    event.get("agent_type"), event.get("model"), self._session_status(event),
                    event.get("workspace_hash"), event.get("user_hash"), event["observed_at"],
                    event["observed_at"], event["event_type"], 1,
                    1 if event.get("tool_name") else 0, 1 if event.get("mcp_server") else 0,
                    event.get("duration_ms"),
                ))
        return accepted

    def export_csv(self) -> str:
        data = self.summary()["items"]
        output = io.StringIO()
        fields = ["device_id", "hostname", "os", "kind", "name", "vendor", "running", "first_seen", "last_seen"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in data:
            writer.writerow({key: item.get(key) for key in fields})
        return output.getvalue()

    def export_agent_sessions_csv(self) -> str:
        data = self.summary()["session_items"]
        output = io.StringIO()
        fields = [
            "device_id", "hostname", "os", "app", "agent_type", "model", "status",
            "first_seen", "last_seen", "last_event", "event_count", "tool_count", "mcp_count",
            "duration_ms",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in data:
            writer.writerow({key: item.get(key) for key in fields})
        return output.getvalue()
