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
from .detector import collect_inventory, digest
from .runtime import claim_spool, read_events, spool_path


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


class AgentClient:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = json.loads(config_path.read_text())

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
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"server returned HTTP {exc.code}: {detail}") from exc

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

    def scan_payload(self) -> dict[str, Any]:
        assets = [asset.to_dict() for asset in collect_inventory()]
        return {
            "schema_version": 1,
            "scan_id": str(uuid.uuid4()),
            "observed_at": _utc_now(),
            "device": device_metadata(),
            "assets": assets,
            "privacy": {
                "content_captured": False,
                "secrets_captured": False,
                "paths_hashed": True,
                "command_lines_hashed": True,
            },
        }

    def send_once(self) -> dict[str, Any]:
        if not self.config.get("device_token"):
            self.enroll()
        inventory = self._request("/api/v1/reports", self.scan_payload(), self.config["device_token"])
        runtime_count = self.flush_runtime_events()
        inventory["runtime_event_count"] = runtime_count
        return inventory

    def flush_runtime_events(self) -> int:
        path = spool_path(self.config_path, self.config)
        pending = claim_spool(path)
        if pending is None:
            return 0
        events = read_events(pending)
        if not events:
            pending.unlink(missing_ok=True)
            return 0
        self._request(
            "/api/v1/runtime-events",
            {"schema_version": 1, "events": events},
            self.config["device_token"],
        )
        pending.unlink(missing_ok=True)
        return len(events)

    def run(self) -> None:
        interval = max(60, int(self.config.get("scan_interval_seconds", 300)))
        backoff = 5
        while True:
            try:
                result = self.send_once()
                print(
                    f"inventory accepted: {result.get('asset_count', 0)} assets; "
                    f"{result.get('runtime_event_count', 0)} runtime events",
                    flush=True,
                )
                backoff = 5
                time.sleep(interval)
            except Exception as exc:
                print(f"inventory upload failed: {exc}", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, interval)


def write_example_config(path: Path) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps({
        "server_url": "https://inventory.example.com",
        "enrollment_token": "replace-me",
        "scan_interval_seconds": 300,
    }, indent=2) + "\n")
    if os.name != "nt":
        path.chmod(0o600)
