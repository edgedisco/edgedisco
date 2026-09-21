"""Separately supervised OTLP Logs delivery with bounded HTTP requests."""
from __future__ import annotations

import gzip
import http.client
import json
import random
import signal
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from . import __version__
from .otlp_encoder import encode_outbox_event, OTLP_CONTENT_TYPE

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Result:
    status: str
    code: str | None = None
    http_status: int | None = None
    retry_after: float = 0


def retry_after_seconds(value, now=None):
    try:
        if value is None:
            return 0
        if value.strip().isdigit():
            return min(300, int(value))
        date = parsedate_to_datetime(value)
        return min(300, max(0, (date - (now or datetime.now(timezone.utc))).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return 0


def classify_response(status, body, retry_after=None):
    from google.protobuf.message import DecodeError
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
    if status in (429, 502, 503, 504):
        return Result("retry", "http_retryable", status, retry_after_seconds(retry_after))
    if not 200 <= status < 300:
        return Result("failed", "http_permanent", status)
    if len(body) > MAX_RESPONSE_BYTES:
        return Result("retry", "invalid_response", status)
    try:
        response = ExportLogsServiceResponse.FromString(body)
    except DecodeError:
        return Result("retry", "invalid_response", status)
    if response.partial_success.rejected_log_records > 0:
        return Result("failed", "partial_success", status)
    return Result("delivered", http_status=status)


class HttpTransport:
    """No proxies, redirects, cookies, implicit retries, or response-body logging."""
    def __init__(self, config):
        self.config = config
        self.connection = None

    def close(self):
        conn, self.connection = self.connection, None
        if conn:
            self._close(conn)

    @staticmethod
    def _close(conn):
        sock = conn.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()

    def send(self, wire, stop=None):
        config = self.config
        endpoint = urlsplit(config.endpoint)
        if self.connection is None:
            if endpoint.scheme == "https":
                self.connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port, timeout=config.timeout, context=config.context)
            else:
                self.connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=config.timeout)
        conn = self.connection
        conn.auto_open = 0  # A cancelled request must not reopen the connection.
        headers = {**config.headers, "Content-Type": OTLP_CONTENT_TYPE, "User-Agent": f"edgedisco/{__version__}"}
        if config.compression == "gzip":
            wire = gzip.compress(wire, mtime=0)
            headers["Content-Encoding"] = "gzip"
        complete, cancelled = threading.Event(), threading.Event()
        result = [Result("retry", "transport")]
        deadline = time.monotonic() + config.timeout
        active_socket = [None]

        def cancel():
            cancelled.set()
            if active_socket[0] is not None:
                try:
                    active_socket[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.close()

        def request():
            try:
                if conn.sock is None:
                    conn.connect()
                active_socket[0] = conn.sock
                # DNS may outlive the caller's deadline. Never POST after cancellation.
                if cancelled.is_set() or time.monotonic() >= deadline:
                    return
                conn.request("POST", endpoint.path or "/", wire, headers)
                response = conn.getresponse()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                result[0] = classify_response(response.status, body, response.getheader("Retry-After"))
                if len(body) > MAX_RESPONSE_BYTES:
                    self._close(conn)
            except (OSError, http.client.HTTPException, ValueError):
                result[0] = Result("retry", "transport")
                self._close(conn)
            finally:
                if cancelled.is_set():
                    self._close(conn)
                complete.set()

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        stop_deadline = None
        while not complete.wait(0.02):
            now = time.monotonic()
            if stop is not None and stop.is_set() and stop_deadline is None:
                stop_deadline = now + config.shutdown_grace
            if stop_deadline is not None and now >= stop_deadline:
                cancel()
                return None  # Leave ownership for lease recovery on shutdown.
            if now >= deadline:
                cancel()
                return Result("retry", "transport")
        return result[0]


class Exporter:
    def __init__(self, store, config, transport=None, stop=None):
        # Fail before claiming rows if the optional encoder dependency is absent.
        try:
            from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
        except ImportError:
            raise RuntimeError("OTLP export requires ai-asset-inventory[otlp]") from None
        self.store, self.config = store, config
        self.transport = transport or HttpTransport(config)
        self.stop = stop or threading.Event()

    def run_once(self):
        if self.stop.is_set():
            return 0
        rows = self.store.claim(self.config)
        groups, group, wire = [], [], b""
        for row in rows:
            try:
                encoded = encode_outbox_event(row["payload_json"])
            except (ValueError, TypeError, OverflowError):
                self.store.finish([row], "failed", "invalid_payload")
                continue
            # Concatenating protobuf messages merges repeated resource_logs fields.
            if group and len(wire) + len(encoded) > self.config.batch_bytes:
                groups.append((group, wire))
                group, wire = [], b""
            group.append(row)
            wire += encoded
        if group:
            groups.append((group, wire))
        for group, wire in groups:
            if self.stop.is_set():
                break
            if not self.store.begin_attempt(group, self.config):
                continue
            result = self.transport.send(wire, self.stop)
            if result is None:
                break
            delay = 0
            if result.status == "retry":
                attempt = max(row["attempt_count"] for row in group)
                delay = max(result.retry_after, random.uniform(0, min(60, 2 ** min(attempt - 1, 6))))
            count = self.store.finish(group, result.status, result.code, result.http_status, delay)
            # Only fixed categories and aggregate counts reach process logs.
            print(json.dumps({"events": count, "status": result.status, "code": result.code,
                              "http_status": result.http_status}), flush=True)
        self.store.cleanup(self.config)
        return len(rows)

    def run(self, *, once=False):
        prior = {}
        def stopping(signum, frame):
            self.stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            prior[sig] = signal.signal(sig, stopping)
        try:
            while not self.stop.is_set():
                try:
                    count = self.run_once()
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise RuntimeError("Exporter database operation failed") from None
                    count = 0
                if once:
                    break
                if not count:
                    self.stop.wait(self.config.poll_interval)
        finally:
            self.transport.close()
            for sig, handler in prior.items():
                signal.signal(sig, handler)
