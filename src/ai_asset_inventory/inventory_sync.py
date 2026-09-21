"""Stable, privacy-safe inventory snapshot and change feed for external consumers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from .otlp_events import project_asset


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_inventory_asset(device_id: str, observed_at: str,
                             asset: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Project an asset for sync; MCP configuration has a separate safe schema from OTLP."""
    if asset.get("kind") != "mcp_server":
        return project_asset(device_id, observed_at, asset)
    name = asset.get("name")
    vendor = asset.get("vendor")
    version = asset.get("version")
    running = asset.get("running")
    metadata = asset.get("metadata", {})
    if (not isinstance(device_id, str) or not device_id
            or not isinstance(name, str) or not name or len(name) > 255
            or not isinstance(vendor, str) or not vendor or len(vendor) > 255
            or version is not None and (not isinstance(version, str) or not version or len(version) > 128)
            or not isinstance(running, bool) or not isinstance(metadata, dict)):
        return None
    try:
        instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return None
    except (AttributeError, ValueError):
        return None
    demo_marker = metadata.get("demo_lab")
    if demo_marker is not None and not isinstance(demo_marker, bool):
        return None
    simulated = demo_marker is True
    if simulated and metadata.get("evidence_label") != "SIMULATED TEST WORKLOADS":
        return None
    attributes: dict[str, Any] = {
        "edgedisco.schema.version": 1,
        "device.id": device_id,
        "asset.kind": "mcp_server",
        "asset.name": name,
        "asset.vendor": vendor,
        "asset.running": running,
        "edgedisco.simulated": simulated,
    }
    if version is not None:
        attributes["asset.version"] = version
    configured_in = metadata.get("configured_in")
    if configured_in in {"Claude Desktop", "Cursor", "VS Code"}:
        attributes["asset.configured_in"] = configured_in
    transport = metadata.get("transport")
    if transport in {"stdio", "remote"}:
        attributes["asset.transport"] = transport
    executable = metadata.get("executable")
    if (isinstance(executable, str) and executable and len(executable) <= 255
            and "/" not in executable and "\\" not in executable):
        attributes["asset.executable"] = executable
    identity = {
        "schema": 1,
        "device.id": device_id,
        "asset.kind": "mcp_server",
        "asset.name": name,
        "asset.vendor": vendor,
        "asset.configured_in": attributes.get("asset.configured_in"),
        "edgedisco.simulated": simulated,
    }
    state = {
        key: value for key, value in attributes.items()
        if key not in {"edgedisco.schema.version", "device.id", "asset.kind", "asset.name",
                       "asset.vendor", "asset.configured_in", "edgedisco.simulated"}
    }
    event = {
        "timestamp": instant.isoformat(),
        "event.name": "edgedisco.asset.observed",
        "resource": {"service.name": "edgedisco"},
        "attributes": attributes,
    }
    return _digest(identity), _digest(state), event


def bootstrap(conn: sqlite3.Connection) -> None:
    """Reconcile retained current inventory into the sync projection."""
    # Historical rows survive deletion. Only the latest scan's timestamp can
    # establish membership; equal-timestamp scans may leave an ambiguous union.
    # In that case wait for a fresh upload instead of resurrecting old assets.
    rows = conn.execute("""WITH ranked_scans AS (
            SELECT device_id,observed_at,asset_count,
                ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY
                    observed_at DESC,received_at DESC,rowid DESC) AS rank
            FROM scans
            WHERE julianday(observed_at)<=julianday(received_at,'+5 minutes')
        ), latest AS (
            SELECT device_id,observed_at,asset_count FROM ranked_scans WHERE rank=1
        )
        SELECT a.device_id,a.kind,a.name,a.vendor,a.running,a.last_seen,a.metadata_json
        FROM assets a
        JOIN latest ON latest.device_id=a.device_id AND latest.observed_at=a.last_seen
        WHERE latest.asset_count=(SELECT COUNT(*) FROM assets current
            WHERE current.device_id=a.device_id AND current.last_seen=latest.observed_at)
        ORDER BY a.device_id,a.fingerprint DESC""").fetchall()
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
        projected = _project_inventory_asset(device_id, observed_at, asset)
        if projected is None:
            continue
        key, state_hash, event = projected
        old_projection = current.get(key)
        if (old_projection is not None
                and (old_projection[1]["attributes"]["asset.running"]
                     or not event["attributes"]["asset.running"])):
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
