from __future__ import annotations

import json
import platform
import socket
import ssl
import time
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .detector import InventoryScanner, collect_inventory, digest
from .runtime import MAX_BATCH_EVENTS, _file_lock, claim_spool, read_events, spool_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def device_metadata() -> dict[str, str]:
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "agent_version": __version__,
    }


class UploadError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"server returned HTTP {status}")


class AgentClient:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text())
        self.scanner = InventoryScanner(
            static_refresh_seconds=int(self.config.get("static_scan_interval_seconds", 900)),
        )

    def _request(self, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
        url = self.config["server_url"].rstrip("/") + path
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        context = None
        if self.config.get("ca_file"):
            context = ssl.create_default_context(cafile=self.config["ca_file"])
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise UploadError(status) from exc

    def enroll(self) -> None:
        token = self.config.get("enrollment_token")
        if not token:
            raise RuntimeError("config requires enrollment_token for first enrollment")
        result = self._request("/api/v1/enroll", device_metadata(), token)
        self.config["device_id"] = result["device_id"]
        self.config["device_token"] = result["device_token"]
        self.config.pop("enrollment_token", None)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.config, indent=2) + "\n")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.config_path)

    def scan_payload(self, assets: list[Any] | None = None) -> dict[str, Any]:
        observed = collect_inventory() if assets is None else assets
        serialized = [asset.to_dict() if hasattr(asset, "to_dict") else dict(asset) for asset in observed]
        return {
            "schema_version": 2,
            "scan_id": str(uuid.uuid4()),
            "observed_at": _utc_now(),
            "device": device_metadata(),
            "assets": serialized,
            "privacy": {
                "content_captured": False,
                "secrets_captured": False,
                "paths_hashed": True,
                "command_lines_hashed": True,
                "binary_contents_hashed": True,
            },
        }

    def send_once(self, assets: list[Any] | None = None) -> dict[str, Any]:
        if not self.config.get("device_token"):
            self.enroll()
        inventory = self._request("/api/v1/reports", self.scan_payload(assets), self.config["device_token"])
        runtime_count = self.flush_runtime_events()
        inventory["runtime_event_count"] = runtime_count
        return inventory

    def submit_assets(self, assets: list[Any]) -> dict[str, Any]:
        """Upload a prepared asset inventory through the normal report API."""
        if not self.config.get("device_token"):
            self.enroll()
        payload = {
            "schema_version": 2,
            "scan_id": str(uuid.uuid4()),
            "observed_at": _utc_now(),
            "device": device_metadata(),
            "assets": [
                asset.to_dict() if hasattr(asset, "to_dict") else dict(asset)
                for asset in assets
            ],
            "privacy": {
                "content_captured": False,
                "secrets_captured": False,
                "paths_hashed": True,
                "command_lines_hashed": True,
                "binary_contents_hashed": True,
            },
        }
        return self._request("/api/v1/reports", payload, self.config["device_token"])

    def flush_runtime_events(self) -> int:
        path = spool_path(self.config_path, self.config)
        # Serialize consumers without holding the hook writers' lock over HTTP.
        with _file_lock(path.with_suffix(path.suffix + ".upload")):
            return self._flush_runtime_events(path)

    def _flush_runtime_events(self, path: Path) -> int:
        pending = claim_spool(path)
        if pending is None:
            return 0
        events = read_events(pending, limit=None)
        if not events:
            pending.unlink(missing_ok=True)
            return 0
        batch = []
        batch_bytes = 0
        for event in events:
            size = len(json.dumps(event).encode()) + 2
            if batch and (len(batch) == MAX_BATCH_EVENTS or batch_bytes + size > 1_800_000):
                self._send_runtime_batch(batch)
                batch, batch_bytes = [], 0
            batch.append(event)
            batch_bytes += size
        if batch:
            self._send_runtime_batch(batch)
        # On failure retain the whole file; event IDs make replay idempotent.
        pending.unlink(missing_ok=True)
        return len(events)

    def _send_runtime_batch(self, events: list[dict[str, Any]]) -> None:
        self._request(
            "/api/v1/runtime-events",
            {"schema_version": 1, "events": events},
            self.config["device_token"],
        )

    def run(self) -> None:
        report_interval = max(60, int(self.config.get("scan_interval_seconds", 300)))
        poll_interval = max(
            10,
            min(report_interval, int(self.config.get("process_poll_interval_seconds", 60))),
        )
        backoff = 5
        last_report = 0.0
        last_state: str | None = None
        while True:
            try:
                assets = self.scanner.collect_inventory()
                state = _inventory_state(assets)
                now = time.monotonic()
                if state != last_state or now - last_report >= report_interval:
                    result = self.send_once(assets)
                    last_state = state
                    last_report = now
                    print(
                        f"inventory accepted: {result.get('asset_count', 0)} assets; "
                        f"{result.get('runtime_event_count', 0)} runtime events",
                        flush=True,
                    )
                else:
                    self.flush_runtime_events()
                backoff = 5
                time.sleep(poll_interval)
            except Exception as exc:
                print(f"inventory upload failed: {exc}", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, report_interval)


def _inventory_state(assets: list[Any]) -> str:
    serialized = [asset.to_dict() if hasattr(asset, "to_dict") else dict(asset) for asset in assets]
    serialized.sort(key=lambda item: str(item.get("fingerprint", "")))
    return digest(json.dumps(serialized, sort_keys=True, separators=(",", ":")))


def write_example_config(path: Path) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps({
        "server_url": "https://inventory.example.com",
        "enrollment_token": "replace-me",
        "scan_interval_seconds": 300,
        "process_poll_interval_seconds": 60,
        "static_scan_interval_seconds": 900,
    }, indent=2) + "\n")
    if os.name != "nt":
        path.chmod(0o600)
