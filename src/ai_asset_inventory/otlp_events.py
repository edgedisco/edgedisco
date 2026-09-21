"""Strict, transport-independent projection of inventory evidence for OTLP."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .detector import HOST_APP_NAMES, SIGNATURES

SCHEMA_VERSION = 1
EVENT_NAME = "edgedisco.asset.observed"
_KNOWN_ASSETS = {name: vendor for name, vendor, _ in SIGNATURES}
_RELATIONSHIPS = {"spawned_by", "local_process"}


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def project_asset(device_id: str, observed_at: str, asset: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Return (logical key, state hash, allowlisted event), or skip unsafe evidence."""
    kind = asset.get("kind")
    name = asset.get("name")
    vendor = asset.get("vendor")
    version = asset.get("version")
    running = asset.get("running")
    metadata = asset.get("metadata", {})
    if (not isinstance(device_id, str) or not device_id or
            not isinstance(kind, str) or kind not in {"application", "process", "agent_runtime"} or
            not isinstance(name, str) or _KNOWN_ASSETS.get(name) != vendor or
            (version is not None and (not isinstance(version, str) or not version or len(version) > 128)) or
            not isinstance(running, bool) or not isinstance(metadata, dict)):
        return None
    try:
        instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return None
    except (AttributeError, ValueError):
        return None

    # Conflicting or malformed demo markers are not safe to label as real.
    demo_marker = metadata.get("demo_lab")
    if demo_marker is not None and not isinstance(demo_marker, bool):
        return None
    simulated = demo_marker is True
    if simulated and metadata.get("evidence_label") != "SIMULATED TEST WORKLOADS":
        return None
    if not simulated and ("evidence_label" in metadata or metadata.get("observed_running") is True):
        return None

    attributes: dict[str, Any] = {
        "edgedisco.schema.version": SCHEMA_VERSION,
        "device.id": device_id,
        "asset.kind": kind,
        "asset.name": name,
        "asset.vendor": vendor,
        "asset.running": running,
        "edgedisco.simulated": simulated,
    }
    if version is not None:
        attributes["asset.version"] = version
    host_app = None
    relationship = None
    if kind == "agent_runtime":
        host_app = metadata.get("host_app")
        relationship = metadata.get("relationship")
        if not isinstance(host_app, str) or host_app not in HOST_APP_NAMES | {"Direct/local"}:
            return None
        expected = "local_process" if host_app == "Direct/local" else "spawned_by"
        if relationship not in _RELATIONSHIPS or relationship != expected:
            return None
        attributes["asset.host_app"] = host_app
        attributes["asset.relationship"] = relationship

    logical_key = _digest({
        "schema": SCHEMA_VERSION, "device.id": device_id, "asset.kind": kind,
        "asset.name": name, "asset.vendor": vendor,
        "asset.host_app": host_app, "edgedisco.simulated": simulated,
    })
    state_hash = _digest({
        "asset.running": running,
        "asset.relationship": relationship,
        "asset.version": version,
    })
    event = {
        "timestamp": instant.isoformat(),
        "event.name": EVENT_NAME,
        "resource": {"service.name": "edgedisco"},
        "attributes": attributes,
    }
    return logical_key, state_hash, event
