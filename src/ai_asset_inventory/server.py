from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from .database import Database
from .runtime import validate_normalized_event


class InventoryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], database: Database, admin_token: str, enrollment_token: str):
        super().__init__(address, RequestHandler)
        self.database = database
        self.admin_token = admin_token
        self.enrollment_token = enrollment_token
        self.session_secret = secrets.token_bytes(32)


class RequestHandler(BaseHTTPRequestHandler):
    server: InventoryServer
    server_version = "AIInventory/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self, limit: int = 2_000_000) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > limit:
            raise ValueError("request too large")
        return self.rfile.read(length)

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    def _session_cookie(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("aai_session")
        return morsel.value if morsel else None

    def _valid_session(self) -> bool:
        raw = self._session_cookie()
        if not raw or "." not in raw:
            return False
        expiry, signature = raw.split(".", 1)
        try:
            if int(expiry) < int(time.time()):
                return False
        except ValueError:
            return False
        expected = hmac.new(self.server.session_secret, expiry.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _admin(self) -> bool:
        return hmac.compare_digest(self._bearer(), self.server.admin_token) or self._valid_session()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/api/v1/summary":
            if not self._admin():
                return self._json(401, {"error": "unauthorized"})
            self._json(200, self.server.database.summary())
        elif path == "/api/v1/export.csv":
            if not self._admin():
                return self._json(401, {"error": "unauthorized"})
            body = self.server.database.export_csv().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=ai-asset-inventory.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/v1/agent-sessions.csv":
            if not self._admin():
                return self._json(401, {"error": "unauthorized"})
            body = self.server.database.export_agent_sessions_csv().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=edgedisco-agent-sessions.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/":
            body = files("ai_asset_inventory").joinpath("dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/login":
                form = urllib.parse.parse_qs(self._body().decode())
                supplied = form.get("token", [""])[0]
                if not hmac.compare_digest(supplied, self.server.admin_token):
                    return self._json(401, {"error": "invalid admin token"})
                expiry = str(int(time.time()) + 8 * 60 * 60)
                signature = hmac.new(self.server.session_secret, expiry.encode(), hashlib.sha256).hexdigest()
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"aai_session={expiry}.{signature}; HttpOnly; SameSite=Strict; Path=/")
                self.end_headers()
                return
            if path == "/api/v1/enroll":
                if not hmac.compare_digest(self._bearer(), self.server.enrollment_token):
                    return self._json(401, {"error": "invalid enrollment token"})
                payload = json.loads(self._body())
                device_id, token = self.server.database.enroll(payload)
                return self._json(201, {"device_id": device_id, "device_token": token})
            if path == "/api/v1/reports":
                device = self.server.database.device_for_token(self._bearer())
                if not device:
                    return self._json(401, {"error": "invalid device token"})
                payload = json.loads(self._body())
                self._validate_report(payload)
                count = self.server.database.ingest(device["id"], payload)
                return self._json(202, {"status": "accepted", "asset_count": count})
            if path == "/api/v1/runtime-events":
                device = self.server.database.device_for_token(self._bearer())
                if not device:
                    return self._json(401, {"error": "invalid device token"})
                payload = json.loads(self._body())
                events = payload.get("events")
                if not isinstance(events, list) or len(events) > 1000:
                    raise ValueError("events must be a list with at most 1000 entries")
                for event in events:
                    if not isinstance(event, dict):
                        raise ValueError("each runtime event must be an object")
                    validate_normalized_event(event)
                count = self.server.database.ingest_runtime_events(device["id"], events)
                return self._json(202, {"status": "accepted", "runtime_event_count": count})
            self._json(404, {"error": "not found"})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})

    @staticmethod
    def _validate_report(payload: dict[str, Any]) -> None:
        for field in ("scan_id", "observed_at", "device", "assets", "privacy"):
            if field not in payload:
                raise ValueError(f"missing field: {field}")
        if not isinstance(payload["assets"], list) or len(payload["assets"]) > 10_000:
            raise ValueError("assets must be a list with at most 10000 entries")
        for index, asset in enumerate(payload["assets"]):
            if not isinstance(asset, dict):
                raise ValueError(f"asset {index} must be an object")
            for field in ("fingerprint", "kind", "name", "vendor", "running"):
                if field not in asset:
                    raise ValueError(f"asset {index} missing field: {field}")
            if not isinstance(asset["running"], bool):
                raise ValueError(f"asset {index} running must be boolean")


def serve(host: str, port: int, db_path: Path) -> None:
    admin = os.environ.get("AAI_ADMIN_TOKEN")
    enroll = os.environ.get("AAI_ENROLLMENT_TOKEN")
    if not admin or not enroll:
        raise RuntimeError("AAI_ADMIN_TOKEN and AAI_ENROLLMENT_TOKEN are required")
    if len(admin) < 24 or len(enroll) < 24:
        raise RuntimeError("AAI_ADMIN_TOKEN and AAI_ENROLLMENT_TOKEN must be at least 24 characters")
    if hmac.compare_digest(admin, enroll):
        raise RuntimeError("administrator and enrollment tokens must be different")
    server = InventoryServer((host, port), Database(db_path), admin, enroll)
    print(f"AI Asset Inventory listening on http://{host}:{port}")
    server.serve_forever()
