"""Transactional outbox delivery; never reads raw inventory tables."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import ClosingConnection, DATABASE_VERSION, Database, utc_now

ERROR_CODES = {"invalid_payload", "partial_success", "invalid_response", "transport",
               "http_retryable", "http_permanent", "lease_expired"}


def migrate_outbox(conn):
    """Called under the inventory/worker's migration transaction."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(otlp_outbox)")}
    if "lease_id" not in columns:
        conn.execute("""CREATE TABLE otlp_outbox_v3 (
            id TEXT PRIMARY KEY, asset_key TEXT NOT NULL, payload_json TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','retry','sending','delivered','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
            created_at TEXT NOT NULL, delivered_at TEXT, last_error_code TEXT,
            lease_id TEXT, lease_expires_at TEXT, last_attempt_at TEXT, failed_at TEXT,
            last_http_status INTEGER)""")
        conn.execute("""INSERT INTO otlp_outbox_v3
            (id,asset_key,payload_json,payload_bytes,status,attempt_count,next_attempt_at,
             created_at,delivered_at,last_error_code)
            SELECT id,asset_key,payload_json,payload_bytes,status,attempt_count,next_attempt_at,
             created_at,delivered_at,last_error_code FROM otlp_outbox""")
        conn.execute("DROP TABLE otlp_outbox")
        conn.execute("ALTER TABLE otlp_outbox_v3 RENAME TO otlp_outbox")
        conn.execute("CREATE INDEX idx_otlp_outbox_due ON otlp_outbox(status,next_attempt_at)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(otlp_export_status)")}
    for declaration in (
        "attempted_events_total INTEGER NOT NULL DEFAULT 0",
        "retried_events_total INTEGER NOT NULL DEFAULT 0",
        "delivered_events_total INTEGER NOT NULL DEFAULT 0",
        "failed_events_total INTEGER NOT NULL DEFAULT 0",
        "last_attempt_at TEXT", "last_success_at TEXT", "last_failure_at TEXT",
        "last_failure_code TEXT", "last_http_status INTEGER",
    ):
        if declaration.split()[0] not in columns:
            conn.execute("ALTER TABLE otlp_export_status ADD COLUMN " + declaration)


class OutboxStore:
    def __init__(self, path: Path):
        # mode=rw prevents an accidental empty replacement database.
        self.uri = Path(path).resolve().as_uri() + "?mode=rw"
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (2, DATABASE_VERSION):
                    raise RuntimeError("Unsupported exporter database schema; upgrade the inventory server first")
                for table in ("otlp_outbox", "otlp_asset_state", "otlp_export_status"):
                    conn.execute(f"SELECT * FROM {table} LIMIT 0")
                if conn.execute("SELECT COUNT(*) FROM otlp_export_status WHERE id=1").fetchone()[0] != 1:
                    raise RuntimeError("Invalid exporter status metadata")
                if version == 2:
                    migrate_outbox(conn)
                    conn.execute(f"PRAGMA user_version={DATABASE_VERSION}")
                # Validate required delivery columns even when the version is current.
                conn.execute("SELECT lease_id,lease_expires_at,last_attempt_at,failed_at,last_http_status FROM otlp_outbox LIMIT 0")
                conn.execute("SELECT attempted_events_total,retried_events_total,delivered_events_total,failed_events_total FROM otlp_export_status LIMIT 0")
        except sqlite3.DatabaseError:
            raise RuntimeError("Cannot open a valid exporter database") from None

    def connect(self):
        conn = sqlite3.connect(self.uri, uri=True, timeout=0.25, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def claim(self, config):
        now = utc_now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=config.lease_seconds)).isoformat()
        lease = secrets.token_hex(24)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = conn.execute("""UPDATE otlp_outbox SET status='retry',lease_id=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code='lease_expired'
                WHERE status='sending' AND lease_expires_at<=?""", (now, now)).rowcount
            if expired:
                conn.execute("UPDATE otlp_export_status SET retried_events_total=retried_events_total+? WHERE id=1", (expired,))
            # A newer observation may have arrived while an older row was in flight.
            # Retained latest-state metadata survives delivery cleanup, so even a
            # previously delivered replacement prevents replay of the older state.
            superseded = conn.execute("""SELECT id,asset_key FROM otlp_outbox AS queued
                WHERE status IN ('pending','retry') AND NOT EXISTS (
                    SELECT 1 FROM otlp_asset_state AS latest
                    WHERE latest.asset_key=queued.asset_key AND latest.last_outbox_id=queued.id)
                LIMIT 500""").fetchall()
            for row in superseded:
                Database._drop_outbox_row(conn, row, now, "superseded")
            # Expiration is enforced even when inventory ingestion is idle.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            old = conn.execute("SELECT id,asset_key FROM otlp_outbox WHERE status IN ('pending','retry') AND created_at<? LIMIT 500", (cutoff,)).fetchall()
            for row in old:
                conn.execute("DELETE FROM otlp_outbox WHERE id=?", (row["id"],))
                conn.execute("DELETE FROM otlp_asset_state WHERE asset_key=? AND last_outbox_id=?", (row["asset_key"], row["id"]))
            if old:
                conn.execute("""UPDATE otlp_export_status SET dropped_events_total=dropped_events_total+?,
                    last_dropped_at=?,last_drop_reason='age' WHERE id=1""", (len(old), now))
            candidates = conn.execute("""SELECT * FROM otlp_outbox AS queued WHERE status IN ('pending','retry')
                AND next_attempt_at<=? AND created_at>=? AND EXISTS (
                    SELECT 1 FROM otlp_asset_state AS latest
                    WHERE latest.asset_key=queued.asset_key AND latest.last_outbox_id=queued.id)
                ORDER BY created_at,id LIMIT ?""", (now, cutoff, config.batch_records)).fetchall()
            rows, size = [], 0
            for row in candidates:
                if rows and size + row["payload_bytes"] > config.batch_bytes:
                    break
                rows.append(dict(row))
                size += row["payload_bytes"]
            for row in rows:
                conn.execute("""UPDATE otlp_outbox SET status='sending',lease_id=?,lease_expires_at=?
                    WHERE id=?""", (lease, expires, row["id"]))
                row.update(lease_id=lease, lease_expires_at=expires)
        return rows

    def begin_attempt(self, rows, config):
        """Recheck ownership and renew before I/O, after potentially slow encoding."""
        now = utc_now()
        minimum = (datetime.now(timezone.utc) + timedelta(seconds=config.timeout + config.shutdown_grace)).isoformat()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=max(config.lease_seconds, config.timeout + config.shutdown_grace + 1))).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                current = conn.execute("SELECT status,lease_id,lease_expires_at FROM otlp_outbox WHERE id=?", (row["id"],)).fetchone()
                if not current or current["status"] != "sending" or current["lease_id"] != row["lease_id"] or current["lease_expires_at"] <= now:
                    return False
            for row in rows:
                conn.execute("""UPDATE otlp_outbox SET attempt_count=attempt_count+1,last_attempt_at=?,
                    lease_expires_at=CASE WHEN lease_expires_at<? THEN ? ELSE lease_expires_at END WHERE id=?""",
                             (now, minimum, expires, row["id"]))
                row["attempt_count"] += 1
            conn.execute("""UPDATE otlp_export_status SET attempted_events_total=attempted_events_total+?,
                last_attempt_at=? WHERE id=1""", (len(rows), now))
        return True

    def finish(self, rows, status, code=None, http_status=None, delay=0):
        if status not in ("delivered", "failed", "retry") or (code is not None and code not in ERROR_CODES):
            raise ValueError("Invalid exporter result")
        now = utc_now()
        due = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = 0
            for row in rows:
                changed = conn.execute("""UPDATE otlp_outbox SET status=?,last_error_code=?,last_http_status=?,
                    next_attempt_at=?,delivered_at=?,failed_at=?,lease_id=NULL,lease_expires_at=NULL
                    WHERE id=? AND status='sending' AND lease_id=? AND lease_expires_at>?""",
                    (status, code, http_status, due, now if status == "delivered" else None,
                     now if status == "failed" else None, row["id"], row["lease_id"], now)).rowcount
                if changed and status == "retry":
                    current = conn.execute("""SELECT 1 FROM otlp_asset_state
                        WHERE asset_key=? AND last_outbox_id=?""", (row["asset_key"], row["id"])).fetchone()
                    if current is None:
                        Database._drop_outbox_row(conn, row, now, "superseded")
                        continue
                count += changed
                if changed and status == "failed":
                    conn.execute("DELETE FROM otlp_asset_state WHERE asset_key=? AND last_outbox_id=?", (row["asset_key"], row["id"]))
            if count:
                counter = {"delivered": "delivered", "failed": "failed", "retry": "retried"}[status]
                conn.execute(f"UPDATE otlp_export_status SET {counter}_events_total={counter}_events_total+?,last_http_status=? WHERE id=1", (count, http_status))
                if status == "delivered":
                    conn.execute("UPDATE otlp_export_status SET last_success_at=? WHERE id=1", (now,))
                else:
                    conn.execute("UPDATE otlp_export_status SET last_failure_at=?,last_failure_code=? WHERE id=1", (now, code))
        return count

    def cleanup(self, config):
        with self.connect() as conn:
            for status, column, days in (("delivered", "delivered_at", config.delivered_days), ("failed", "failed_at", config.failed_days)):
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                conn.execute(f"DELETE FROM otlp_outbox WHERE id IN (SELECT id FROM otlp_outbox WHERE status=? AND COALESCE({column},created_at)<? LIMIT 500)", (status, cutoff))

    def status(self):
        now = utc_now()
        with self.connect() as conn:
            states = {state: {"count": 0, "bytes": 0} for state in ("pending", "retry", "sending", "delivered", "failed")}
            for row in conn.execute("SELECT status,COUNT(*) n,COALESCE(SUM(payload_bytes),0) bytes FROM otlp_outbox GROUP BY status"):
                states[row["status"]] = {"count": row["n"], "bytes": row["bytes"]}
            oldest = conn.execute("SELECT MIN(created_at) FROM otlp_outbox WHERE status IN ('pending','retry') AND next_attempt_at<=?", (now,)).fetchone()[0]
            expired = conn.execute("SELECT COUNT(*) FROM otlp_outbox WHERE status='sending' AND lease_expires_at<=?", (now,)).fetchone()[0]
            totals = dict(conn.execute("SELECT * FROM otlp_export_status WHERE id=1").fetchone())
            totals.pop("id")
        return {"states": states, "oldest_due_age_seconds": max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds()) if oldest else None,
                "expired_leases": expired, **totals}
