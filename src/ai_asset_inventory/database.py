from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .otlp_events import project_asset
from .validation import timestamp, observation_timestamp

OTLP_MAX_PENDING = 5_000
OTLP_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
OTLP_MAX_AGE_DAYS = 7
DATABASE_VERSION = 2

# Runtime uploads do not establish process-inventory freshness.
FRESH_SCAN = """EXISTS (SELECT 1 FROM scans fresh WHERE fresh.device_id=a.device_id
    AND julianday(fresh.received_at)>=julianday('now','-15 minutes')
    AND julianday(fresh.observed_at)<=julianday(fresh.received_at,'+5 minutes')
    AND NOT EXISTS (SELECT 1 FROM scans newer WHERE newer.device_id=a.device_id
        AND newer.observed_at>fresh.observed_at
        AND julianday(newer.observed_at)<=julianday(newer.received_at,'+5 minutes')))"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path: Path, *, otlp_enabled: bool = False,
                 otlp_max_pending: int = OTLP_MAX_PENDING,
                 otlp_max_payload_bytes: int = OTLP_MAX_PAYLOAD_BYTES,
                 otlp_max_age_days: int = OTLP_MAX_AGE_DAYS):
        self.path = path
        self.otlp_enabled = otlp_enabled
        self.otlp_max_pending = max(1, otlp_max_pending)
        self.otlp_max_payload_bytes = max(1, otlp_max_payload_bytes)
        self.otlp_max_age_days = max(1, otlp_max_age_days)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > DATABASE_VERSION:
                raise RuntimeError("Database was created by a newer EdgeDisco release; refusing downgrade")
            conn.executescript("""
                BEGIN IMMEDIATE;
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
                    version TEXT, path_hash TEXT, command_hash TEXT,
                    binary_sha256 TEXT, binary_fingerprint_status TEXT,
                    fingerprint_library_version TEXT, metadata_json TEXT NOT NULL,
                    running INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(device_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name);
                CREATE INDEX IF NOT EXISTS idx_scans_device_received ON scans(device_id,received_at);
                CREATE INDEX IF NOT EXISTS idx_scans_device_observed ON scans(device_id,observed_at);
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
                CREATE TABLE IF NOT EXISTS otlp_outbox (
                    id TEXT PRIMARY KEY, asset_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','retry','delivered','failed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    delivered_at TEXT, last_error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_otlp_outbox_due
                    ON otlp_outbox(status,next_attempt_at);
                CREATE TABLE IF NOT EXISTS otlp_asset_state (
                    asset_key TEXT PRIMARY KEY, state_hash TEXT NOT NULL,
                    last_outbox_id TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS otlp_export_status (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    dropped_events_total INTEGER NOT NULL DEFAULT 0,
                    last_dropped_at TEXT, last_drop_reason TEXT
                );
                INSERT OR IGNORE INTO otlp_export_status(id) VALUES(1);
            """)
            if version < 2:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
                for column in (
                    "binary_sha256 TEXT",
                    "binary_fingerprint_status TEXT",
                    "fingerprint_library_version TEXT",
                ):
                    if column.split()[0] not in columns:
                        conn.execute(f"ALTER TABLE assets ADD COLUMN {column}")
            # DDL and the version marker commit together; failed migrations
            # roll back together.
            conn.execute(f"PRAGMA user_version={DATABASE_VERSION}")

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
        observed_at = observation_timestamp(report["observed_at"])
        received_at = utc_now()
        scan_id = str(report["scan_id"])
        assets = report.get("assets", [])
        with self.connect() as conn:
            # Hold the writer reservation while deciding whether this snapshot
            # is newer, so concurrent uploads cannot both supersede each other.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT device_id,asset_count FROM scans WHERE id=?", (scan_id,)).fetchone()
            if existing:
                if existing["device_id"] != device_id:
                    raise ValueError("scan ID already belongs to another device")
                return int(existing["asset_count"])
            newer = conn.execute("SELECT 1 FROM scans WHERE device_id=? AND observed_at>? "
                                 "AND julianday(observed_at)<=julianday(received_at,'+5 minutes') LIMIT 1",
                                 (device_id, observed_at)).fetchone()
            conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (received_at, device_id))
            conn.execute(
                "INSERT INTO scans VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, device_id, observed_at, received_at, len(assets),
                 json.dumps(report.get("privacy", {}), sort_keys=True)),
            )
            if newer:
                return len(assets)
            conn.execute("UPDATE assets SET running=0 WHERE device_id=?", (device_id,))
            for item in assets:
                conn.execute("""
                    INSERT INTO assets(device_id,fingerprint,kind,name,vendor,version,path_hash,
                      command_hash,binary_sha256,binary_fingerprint_status,
                      fingerprint_library_version,metadata_json,running,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(device_id,fingerprint) DO UPDATE SET
                      kind=excluded.kind,name=excluded.name,vendor=excluded.vendor,
                      version=excluded.version,path_hash=excluded.path_hash,
                      command_hash=excluded.command_hash,binary_sha256=excluded.binary_sha256,
                      binary_fingerprint_status=excluded.binary_fingerprint_status,
                      fingerprint_library_version=excluded.fingerprint_library_version,
                      metadata_json=excluded.metadata_json,
                      running=excluded.running,last_seen=excluded.last_seen
                """, (device_id, str(item.get("fingerprint", ""))[:128],
                      str(item.get("kind", "unknown"))[:64], str(item.get("name", "unknown"))[:255],
                      str(item.get("vendor", "Unknown"))[:255], item.get("version"),
                      item.get("path_hash"), item.get("command_hash"),
                      item.get("binary_sha256"), item.get("binary_fingerprint_status"),
                      item.get("fingerprint_library_version"),
                      json.dumps(item.get("metadata", {}), sort_keys=True),
                      1 if item.get("running") else 0, observed_at, observed_at))
            if self.otlp_enabled:
                conn.execute("SAVEPOINT otlp_projection")
                try:
                    reconciled = []
                    for row in conn.execute("SELECT * FROM assets WHERE device_id=?", (device_id,)):
                        item = dict(row)
                        item["running"] = bool(item["running"])
                        item["metadata"] = json.loads(item.pop("metadata_json"))
                        reconciled.append(item)
                    self._queue_otlp_observations(conn, device_id, observed_at, reconciled)
                    conn.execute("RELEASE SAVEPOINT otlp_projection")
                except (sqlite3.Error, TypeError, ValueError):
                    # Export bookkeeping must not roll back local inventory.
                    conn.execute("ROLLBACK TO SAVEPOINT otlp_projection")
                    conn.execute("RELEASE SAVEPOINT otlp_projection")
        return len(assets)

    @staticmethod
    def _drop_outbox_row(conn: sqlite3.Connection, row: sqlite3.Row, now: str, reason: str) -> None:
        conn.execute("DELETE FROM otlp_outbox WHERE id=?", (row["id"],))
        conn.execute("DELETE FROM otlp_asset_state WHERE asset_key=? AND last_outbox_id=?",
                     (row["asset_key"], row["id"]))
        conn.execute("""UPDATE otlp_export_status SET dropped_events_total=dropped_events_total+1,
                     last_dropped_at=?,last_drop_reason=? WHERE id=1""", (now, reason))

    def _queue_otlp_observations(self, conn: sqlite3.Connection, device_id: str,
                                 observed_at: str, assets: list[dict[str, Any]]) -> None:
        now = utc_now()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.otlp_max_age_days)).isoformat()
        expired = conn.execute("""SELECT id,asset_key FROM otlp_outbox
            WHERE status IN ('pending','retry') AND created_at<? ORDER BY created_at,id""", (cutoff,)).fetchall()
        for row in expired:
            self._drop_outbox_row(conn, row, now, "age")

        projections = {}
        for asset in assets:
            projected = project_asset(device_id, observed_at, asset)
            if projected is None:
                continue
            key, _, event = projected
            # Multiple raw fingerprints can describe one exported logical asset.
            if key not in projections or event["attributes"]["asset.running"]:
                projections[key] = projected
        for projected in projections.values():
            asset_key, state_hash, event = projected
            prior = conn.execute("SELECT state_hash FROM otlp_asset_state WHERE asset_key=?", (asset_key,)).fetchone()
            if prior and prior["state_hash"] == state_hash:
                continue
            event_id = "sha256:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            event["attributes"]["edgedisco.observation.id"] = event_id
            payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
            payload_bytes = len(payload.encode("utf-8"))
            if payload_bytes > self.otlp_max_payload_bytes:
                conn.execute("""UPDATE otlp_export_status SET dropped_events_total=dropped_events_total+1,
                    last_dropped_at=?,last_drop_reason='oversize' WHERE id=1""", (now,))
                continue
            # Superseded pending states have less value than the latest observation.
            superseded = conn.execute("""SELECT id,asset_key FROM otlp_outbox
                WHERE asset_key=? AND status IN ('pending','retry') ORDER BY created_at,id""", (asset_key,)).fetchall()
            for row in superseded:
                self._drop_outbox_row(conn, row, now, "superseded")
            while True:
                count, size = conn.execute("""SELECT COUNT(*),COALESCE(SUM(payload_bytes),0)
                    FROM otlp_outbox WHERE status IN ('pending','retry')""").fetchone()
                if count < self.otlp_max_pending and size + payload_bytes <= self.otlp_max_payload_bytes:
                    break
                oldest = conn.execute("""SELECT id,asset_key FROM otlp_outbox
                    WHERE status IN ('pending','retry') ORDER BY created_at,id LIMIT 1""").fetchone()
                if oldest is None:
                    break
                self._drop_outbox_row(conn, oldest, now, "capacity")
            conn.execute("""INSERT INTO otlp_outbox
                (id,asset_key,payload_json,payload_bytes,status,next_attempt_at,created_at)
                VALUES(?,?,?,?,?,?,?)""", (event_id, asset_key, payload, payload_bytes, "pending", now, now))
            conn.execute("""INSERT INTO otlp_asset_state(asset_key,state_hash,last_outbox_id,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(asset_key) DO UPDATE SET
                state_hash=excluded.state_hash,last_outbox_id=excluded.last_outbox_id,
                updated_at=excluded.updated_at""", (asset_key, state_hash, event_id, now))

    def otlp_outbox_status(self) -> dict[str, Any]:
        """Return non-sensitive queue health for future CLI/status presentation."""
        with self.connect() as conn:
            counts = {row["status"]: row["count"] for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM otlp_outbox GROUP BY status"
            )}
            status = conn.execute("SELECT * FROM otlp_export_status WHERE id=1").fetchone()
        return {
            "enabled": self.otlp_enabled,
            "pending": counts.get("pending", 0),
            "retry": counts.get("retry", 0),
            "delivered": counts.get("delivered", 0),
            "failed": counts.get("failed", 0),
            "dropped_events_total": status["dropped_events_total"],
            "last_dropped_at": status["last_dropped_at"],
            "last_drop_reason": status["last_drop_reason"],
            "degraded": status["dropped_events_total"] > 0,
        }

    def summary(self) -> dict[str, Any]:
        active_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        with self.connect() as conn:
            devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            running = conn.execute(f"SELECT COUNT(*) FROM assets a WHERE running=1 AND {FRESH_SCAN}").fetchone()[0]
            mcp = conn.execute("SELECT COUNT(*) FROM assets WHERE kind='mcp_server'").fetchone()[0]
            agents = conn.execute(f"SELECT COUNT(*) FROM assets a WHERE kind='agent_runtime' AND running=1 AND {FRESH_SCAN}").fetchone()[0]
            active_sessions = conn.execute(
                "SELECT COUNT(*) FROM agent_sessions WHERE status='active' AND last_seen>=?",
                (active_cutoff,),
            ).fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
            recent = [dict(row) for row in conn.execute(f"""
                SELECT a.name,a.vendor,a.kind,(a.running AND {FRESH_SCAN}) AS running,
                       (NOT {FRESH_SCAN}) AS stale,a.first_seen,a.last_seen,
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
            row["stale"] = bool(row["stale"])
        for row in event_items:
            row["metadata"] = json.loads(row.pop("metadata_json"))
        return {
            "devices": devices, "assets": assets, "running": running,
            "active_agents": agents, "active_sessions": active_sessions,
            "agent_sessions": session_count, "mcp_servers": mcp,
            "items": recent, "session_items": session_items, "event_items": event_items,
        }

    @staticmethod
    def _result_limit(limit: int) -> int:
        return max(1, min(int(limit), 500))

    def compliance_counts(self) -> dict[str, int]:
        summary = self.summary()
        return {
            key: int(summary[key])
            for key in (
                "devices", "assets", "running", "active_agents", "active_sessions",
                "agent_sessions", "mcp_servers",
            )
        }

    def list_devices(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT id AS device_id,hostname,os,os_version,machine,agent_version,
                       enrolled_at,last_seen
                FROM devices ORDER BY last_seen DESC LIMIT ?
            """, (self._result_limit(limit),))
            return [dict(row) for row in rows]

    def list_assets(self, *, kind: str | None = None, running_only: bool = False,
                    limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if kind:
            clauses.append("a.kind=?")
            parameters.append(str(kind)[:64])
        if running_only:
            clauses.append(f"a.running=1 AND {FRESH_SCAN}")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(self._result_limit(limit))
        with self.connect() as conn:
            rows = conn.execute(f"""
                SELECT a.device_id,d.hostname,d.os,a.kind,a.name,a.vendor,a.version,
                       (a.running AND {FRESH_SCAN}) AS running,(NOT {FRESH_SCAN}) AS stale,a.first_seen,a.last_seen
                FROM assets a JOIN devices d ON d.id=a.device_id
                {where} ORDER BY a.last_seen DESC LIMIT ?
            """, parameters)
            result = [dict(row) for row in rows]
        for row in result:
            row["running"] = bool(row["running"])
            row["stale"] = bool(row["stale"])
        return result

    def list_agent_sessions(self, *, status: str | None = None,
                            app: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        active_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        clauses: list[str] = []
        parameters: list[Any] = [active_cutoff]
        if status:
            clauses.append("s.status=?")
            parameters.append(str(status)[:32])
        if app:
            clauses.append("s.app=?")
            parameters.append(str(app)[:64])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(self._result_limit(limit))
        with self.connect() as conn:
            rows = conn.execute(f"""
                SELECT s.device_id,d.hostname,d.os,s.app,s.agent_type,s.model,
                       CASE WHEN s.status='active' AND s.last_seen<? THEN 'stale' ELSE s.status END AS status,
                       s.first_seen,s.last_seen,s.last_event,s.event_count,s.tool_count,
                       s.mcp_count,s.duration_ms
                FROM agent_sessions s JOIN devices d ON d.id=s.device_id
                {where} ORDER BY s.last_seen DESC LIMIT ?
            """, parameters)
            return [dict(row) for row in rows]

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
                event = dict(event, observed_at=timestamp(event["observed_at"]))
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
                      app=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN excluded.app ELSE agent_sessions.app END,
                      agent_type=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN COALESCE(excluded.agent_type,agent_sessions.agent_type) ELSE COALESCE(agent_sessions.agent_type,excluded.agent_type) END,
                      model=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN COALESCE(excluded.model,agent_sessions.model) ELSE agent_sessions.model END,
                      status=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN excluded.status ELSE agent_sessions.status END,
                      workspace_hash=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN COALESCE(excluded.workspace_hash,agent_sessions.workspace_hash) ELSE COALESCE(agent_sessions.workspace_hash,excluded.workspace_hash) END,
                      user_hash=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN COALESCE(excluded.user_hash,agent_sessions.user_hash) ELSE COALESCE(agent_sessions.user_hash,excluded.user_hash) END,
                      first_seen=MIN(agent_sessions.first_seen,excluded.first_seen),
                      last_seen=MAX(agent_sessions.last_seen,excluded.last_seen),
                      last_event=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN excluded.last_event ELSE agent_sessions.last_event END,
                      duration_ms=CASE WHEN excluded.last_seen>=agent_sessions.last_seen THEN COALESCE(excluded.duration_ms,agent_sessions.duration_ms) ELSE agent_sessions.duration_ms END,
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
        output = io.StringIO()
        fields = [
            "device_id", "hostname", "os", "kind", "name", "vendor", "version",
            "binary_sha256", "binary_fingerprint_status", "fingerprint_library_version",
            "running", "first_seen", "last_seen", "stale",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        with self.connect() as conn:
            for row in conn.execute(f"""SELECT a.device_id,d.hostname,d.os,a.kind,a.name,a.vendor,a.version,
                a.binary_sha256,a.binary_fingerprint_status,a.fingerprint_library_version,
                (a.running AND {FRESH_SCAN}) AS running,a.first_seen,a.last_seen,(NOT {FRESH_SCAN}) AS stale
                FROM assets a JOIN devices d ON d.id=a.device_id ORDER BY a.last_seen DESC"""):
                item = dict(row)
                item["running"] = bool(item["running"])
                item["stale"] = bool(item["stale"])
                writer.writerow(item)
        return output.getvalue()

    def export_agent_sessions_csv(self) -> str:
        output = io.StringIO()
        fields = [
            "device_id", "hostname", "os", "app", "agent_type", "model", "status",
            "first_seen", "last_seen", "last_event", "event_count", "tool_count", "mcp_count",
            "duration_ms",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        with self.connect() as conn:
            for row in conn.execute("""SELECT s.device_id,d.hostname,d.os,s.app,s.agent_type,s.model,
                CASE WHEN s.status='active' AND s.last_seen<? THEN 'stale' ELSE s.status END AS status,
                s.first_seen,s.last_seen,s.last_event,s.event_count,s.tool_count,s.mcp_count,s.duration_ms
                FROM agent_sessions s JOIN devices d ON d.id=s.device_id ORDER BY s.last_seen DESC""", (cutoff,)):
                writer.writerow(dict(row))
        return output.getvalue()
