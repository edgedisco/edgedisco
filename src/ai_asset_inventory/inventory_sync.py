"""Stable, privacy-safe inventory snapshot and change feed for external consumers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .otlp_events import project_asset


def bootstrap(conn: sqlite3.Connection) -> None:
    """Seed an existing installation once from its retained current inventory."""
    if conn.execute("SELECT 1 FROM inventory_sync_changes LIMIT 1").fetchone():
        return
    rows = conn.execute("""SELECT device_id,kind,name,vendor,running,last_seen,metadata_json
        FROM assets ORDER BY device_id,last_seen DESC,fingerprint DESC""").fetchall()
    by_device: dict[str, list[dict[str, Any]]] = {}
    observed: dict[str, str] = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, ValueError):
            continue
        by_device.setdefault(row["device_id"], []).append({
            "kind": row["kind"], "name": row["name"], "vendor": row["vendor"],
            "running": bool(row["running"]), "metadata": metadata,
        })
        observed.setdefault(row["device_id"], row["last_seen"])
    for device_id, assets in by_device.items():
        record_scan_changes(conn, device_id, observed[device_id], assets)


def record_scan_changes(conn: sqlite3.Connection, device_id: str,
                        observed_at: str, assets: list[dict[str, Any]]) -> None:
    current: dict[str, tuple[str, dict[str, Any]]] = {}
    for asset in assets:
        projected = project_asset(device_id, observed_at, asset)
        if projected is None:
            continue
        key, state_hash, event = projected
        if key in current:
            continue
        current[key] = (state_hash, {
            "source": "edgedisco", "source_asset_id": key,
            "observed_at": event["timestamp"], "attributes": event["attributes"],
        })
    previous = {row["asset_key"]: row for row in conn.execute("""
        SELECT c.asset_key,c.state_hash,c.present FROM inventory_sync_changes c
        WHERE c.device_id=? AND c.seq=(SELECT MAX(seq) FROM inventory_sync_changes
            WHERE asset_key=c.asset_key)
    """, (device_id,))}
    for key, (state_hash, record) in current.items():
        old = previous.get(key)
        if old is not None and old["present"] and old["state_hash"] == state_hash:
            continue
        conn.execute("""INSERT INTO inventory_sync_changes
            (asset_key,device_id,state_hash,present,payload_json,observed_at)
            VALUES(?,?,?,?,?,?)""", (key, device_id, state_hash, 1,
                                     json.dumps(record, sort_keys=True), observed_at))
    for key, old in previous.items():
        if old["present"] and key not in current:
            conn.execute("""INSERT INTO inventory_sync_changes
                (asset_key,device_id,state_hash,present,payload_json,observed_at)
                VALUES(?,?,?,?,?,?)""", (key, device_id, old["state_hash"], 0,
                                         None, observed_at))


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, 500)


def _cursor(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("cursor must be a nonnegative integer")
    return value


def snapshot(conn: sqlite3.Connection, *, watermark: int | None = None,
             after: str = "", limit: int = 100) -> dict[str, Any]:
    limit = _limit(limit)
    if watermark is None:
        watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM inventory_sync_changes").fetchone()[0]
    watermark = _cursor(watermark)
    if not isinstance(after, str) or len(after) > 128:
        raise ValueError("after must be an asset ID")
    rows = conn.execute("""SELECT c.asset_key,c.payload_json FROM inventory_sync_changes c
        WHERE c.seq=(SELECT MAX(seq) FROM inventory_sync_changes
            WHERE asset_key=c.asset_key AND seq<=?)
        AND c.present=1 AND c.asset_key>? ORDER BY c.asset_key LIMIT ?""",
        (watermark, after, limit + 1)).fetchall()
    page = rows[:limit]
    return {"watermark": watermark,
            "items": [json.loads(row["payload_json"]) for row in page],
            "next_after": page[-1]["asset_key"] if len(rows) > limit else None}


def changes(conn: sqlite3.Connection, *, cursor: int = 0,
            limit: int = 100) -> dict[str, Any]:
    cursor = _cursor(cursor)
    limit = _limit(limit)
    rows = conn.execute("""SELECT seq,asset_key,present,payload_json,observed_at
        FROM inventory_sync_changes WHERE seq>? ORDER BY seq LIMIT ?""",
        (cursor, limit + 1)).fetchall()
    page = rows[:limit]
    return {"items": [{"cursor": row["seq"], "source_asset_id": row["asset_key"],
                       "operation": "upsert" if row["present"] else "delete",
                       "record": json.loads(row["payload_json"]) if row["present"] else None,
                       "observed_at": row["observed_at"]} for row in page],
            "next_cursor": page[-1]["seq"] if page else cursor,
            "has_more": len(rows) > limit}
