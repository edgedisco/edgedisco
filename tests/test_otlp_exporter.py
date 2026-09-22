import contextlib
import dataclasses
import gzip
import io
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import signal
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.database import Database, utc_now
from ai_asset_inventory.otlp_config import ConfigurationError, ExportConfig
from ai_asset_inventory.otlp_exporter import Exporter, HttpTransport, Result, classify_response, retry_after_seconds, MAX_RESPONSE_BYTES
from ai_asset_inventory.otlp_store import OutboxStore

try:
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest, ExportLogsServiceResponse
except ImportError:
    ExportLogsServiceRequest = None

ENV = {"EDGEDISCO_OTLP_EXPORT_ENABLED": "true", "EDGEDISCO_OTLP_OUTBOX_ENABLED": "true",
       "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:4318/v1/logs"}


class ConfigTests(unittest.TestCase):
    def test_endpoint_precedence_and_prefix(self):
        config = ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://example.org/base"})
        self.assertEqual(config.endpoint, ENV["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"])
        env = {**ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "https://example.org/base/"}
        del env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"]
        self.assertEqual(ExportConfig.from_env(env).endpoint, "https://example.org/base/v1/logs")
        for url in ("http://127.2.3.4:4318/custom", "http://[::1]:4318/custom"):
            self.assertEqual(ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": url}).endpoint, url)

    def test_invalid_configuration_does_not_echo_values(self):
        cases = [("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", url) for url in (
            "http://localhost:4318", "http://192.168.1.1", "https://user:SECRET@example.org",
            "https://example.org?SECRET", "https://example.org#SECRET", "file:///SECRET",
            "http://127.0.0.1:99999", "https://example.org/\nSECRET", "")]
        cases += [("EDGEDISCO_OTLP_OUTBOX_ENABLED", "false"),
                  ("EDGEDISCO_OTLP_EXPORT_ENABLED", "false"),
                  ("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "grpc"),
                  ("OTEL_EXPORTER_OTLP_LOGS_COMPRESSION", "SECRET"),
                  ("EDGEDISCO_OTLP_BATCH_RECORDS", "1001"),
                  ("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "60000")]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ConfigurationError) as exc:
                ExportConfig.from_env({**ENV, key: value})
            self.assertNotIn("SECRET", str(exc.exception))

    def test_headers_and_signal_overrides(self):
        config = ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_HEADERS": "wrong=general",
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "Authorization=Bearer%20SECRET,x-key=a%2Cb%3Dc",
            "OTEL_EXPORTER_OTLP_TIMEOUT": "2000", "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "1234"})
        self.assertEqual(config.headers, {"authorization": "Bearer SECRET", "x-key": "a,b=c"})
        self.assertEqual(config.timeout, 1.234)
        self.assertNotIn("SECRET", repr(config))
        for value in ("Authorization=SECRET,authorization=SECRET", "host=SECRET", "cookie=SECRET",
                      "content-type=SECRET", "x=SECRET%0d%0aInjected", "x=SECRET%zz", "SECRET", "x=SECRET;metadata"):
            with self.assertRaises(ConfigurationError) as exc:
                ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_LOGS_HEADERS": value})
            self.assertNotIn("SECRET", str(exc.exception))

    def test_certificate_and_key_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "SECRET.pem"
            path.write_text("not a certificate")
            for mode, suffix in ((0o666, "CERTIFICATE"), (0o644, "CLIENT_KEY"), (0o600, "CERTIFICATE")):
                path.chmod(mode)
                with self.assertRaises(ConfigurationError) as exc:
                    ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_LOGS_" + suffix: str(path)})
                self.assertNotIn(str(path), str(exc.exception))
            path.chmod(0o600)
            with self.assertRaises(ConfigurationError):
                ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE": str(path)})
            context = ExportConfig.from_env({**ENV, "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://example.org"}).context
            self.assertTrue(context.check_hostname)


class FakeTransport:
    def __init__(self, result=Result("delivered", http_status=200)):
        self.result, self.requests = result, []
    def send(self, wire, stop):
        self.requests.append(wire)
        return self.result
    def close(self):
        pass


@unittest.skipUnless(ExportLogsServiceRequest, "optional OTLP dependency not installed")
class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "inventory.db"
        self.db = Database(self.path, otlp_enabled=True)
        self.config = ExportConfig.from_env(ENV)
        self.store = OutboxStore(self.path)

    def ingest(self, running=True, version="1.2.3", device=None):
        device = device or self.db.enroll({"hostname": "test", "os": "test"})[0]
        self.db.ingest(device, {"scan_id": os.urandom(8).hex(),
            "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            "assets": [{"fingerprint": "test", "kind": "agent_runtime", "name": "CrewAI", "vendor": "CrewAI",
                        "version": version, "running": running, "metadata": {"host_app": "Cursor", "relationship": "spawned_by",
                        "demo_lab": True, "evidence_label": "SIMULATED TEST WORKLOADS", "prompt": "SECRET"}}]})
        return device

    def rows(self):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM otlp_outbox ORDER BY created_at,id")]

    def test_delivery_and_stopped_transition_and_privacy(self):
        device = self.ingest()
        transport = FakeTransport()
        exporter = Exporter(self.store, self.config, transport)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(exporter.run_once(), 2)
            self.ingest(False, device=device)
            exporter.run_once()
        self.assertEqual([r["status"] for r in self.rows()], ["delivered"] * 4)
        self.assertEqual(self.store.status()["delivered_events_total"], 4)
        for wire in transport.requests:
            record = ExportLogsServiceRequest.FromString(wire).resource_logs[0].scope_logs[0].log_records[0]
            self.assertLess(record.time_unix_nano, record.observed_time_unix_nano)
            self.assertNotIn(b"SECRET", wire)
        for sensitive in ("SECRET", device, "CrewAI", "Cursor", self.rows()[0]["id"]):
            self.assertNotIn(sensitive, output.getvalue())

    def test_competing_workers_and_expired_lease_stale_completion(self):
        self.ingest()
        other = OutboxStore(self.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda store: store.claim(self.config), (self.store, other)))
        self.assertEqual(sorted(map(len, claims)), [0, 2])
        original = next(rows for rows in claims if rows)
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00'")
        replacement = other.claim(self.config)
        self.assertNotEqual(original[0]["lease_id"], replacement[0]["lease_id"])
        self.assertFalse(self.store.begin_attempt(original, self.config))
        self.assertEqual(self.store.finish(original, "delivered"), 0)
        self.assertTrue(other.begin_attempt(replacement, self.config))
        self.assertEqual(other.finish(replacement, "delivered"), 2)
        self.assertEqual(other.status()["retried_events_total"], 2)

    def test_retry_survives_restart_and_preserves_id(self):
        self.ingest()
        transport = FakeTransport(Result("retry", "http_retryable", 503, 120))
        exporter = Exporter(self.store, self.config, transport)
        with patch("ai_asset_inventory.otlp_exporter.random.uniform", return_value=0.5):
            exporter.run_once()
        row = self.rows()[0]
        self.assertEqual(row["status"], "retry")
        self.assertEqual(row["attempt_count"], 1)
        self.assertGreater((datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(timezone.utc)).total_seconds(), 119)
        restarted = OutboxStore(self.path)
        self.assertEqual(restarted.claim(self.config), [])
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET next_attempt_at=?", (utc_now(),))
        success = FakeTransport()
        Exporter(restarted, self.config, success).run_once()
        self.assertEqual(success.requests, transport.requests)
        self.assertEqual(self.rows()[0]["id"], row["id"])

    def test_superseded_inflight_retry_and_recovery_are_discarded(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                device = self.ingest()
                old = self.store.claim(self.config)
                self.assertTrue(self.store.begin_attempt(old, self.config))
                self.ingest(False, device=device)
                newer = self.store.claim(self.config)
                self.assertTrue(self.store.begin_attempt(newer, self.config))
                self.store.finish(newer, 'delivered', http_status=200)
                # A delivered replacement may already have been cleaned up.
                with self.db.connect() as conn:
                    conn.execute("DELETE FROM otlp_outbox WHERE status='delivered'")
                if recovery:
                    with self.db.connect() as conn:
                        conn.executemany("UPDATE otlp_outbox SET lease_expires_at='2000-01-01' WHERE id=?", [(r['id'],) for r in old])
                else:
                    self.assertEqual(self.store.finish(old, 'retry', 'transport'), 0)
                self.assertEqual(self.store.claim(self.config), [])
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.store.finish(old, 'delivered'), 0)
                self.assertEqual(self.store.status()['last_drop_reason'], 'superseded')
        self.assertEqual(self.store.status()['dropped_events_total'], 4)

    def test_partial_rejection_terminal_and_dedup_repaired(self):
        device = self.ingest()
        Exporter(self.store, self.config, FakeTransport(Result("failed", "partial_success", 200))).run_once()
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.ingest(device=device)
        self.assertEqual([r["status"] for r in self.rows()], ["failed", "failed", "pending", "pending"])

    def test_invalid_row_isolated_and_batch_limits(self):
        for _ in range(4):
            self.ingest()
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET payload_json='{}' WHERE id=?", (self.rows()[0]["id"],))
        transport = FakeTransport()
        config = dataclasses.replace(self.config, batch_records=3)
        Exporter(self.store, config, transport).run_once()
        self.assertEqual(self.store.status()["states"]["failed"]["count"], 1)
        self.assertEqual(self.store.status()["states"]["pending"]["count"], 5)
        self.assertEqual(len(ExportLogsServiceRequest.FromString(transport.requests[0]).resource_logs), 2)
        # Exact wire bound matters even if the JSON estimate is smaller.
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET status='pending',payload_bytes=1 WHERE status='delivered'")
        transport = FakeTransport()
        Exporter(self.store, dataclasses.replace(config, batch_bytes=600), transport).run_once()
        self.assertTrue(all(len(wire) <= 600 or len(ExportLogsServiceRequest.FromString(wire).resource_logs) == 1 for wire in transport.requests))
        while self.store.status()['states']['pending']['count']:
            Exporter(self.store, config, FakeTransport()).run_once()
        self.ingest()
        # A single oversized valid record is allowed alone.
        transport = FakeTransport()
        Exporter(self.store, dataclasses.replace(config, batch_bytes=1), transport).run_once()
        self.assertEqual(len(transport.requests), 1)
        Exporter(self.store, dataclasses.replace(config, batch_bytes=1), transport).run_once()
        self.assertEqual(len(transport.requests), 2)

    def test_cleanup_retains_active_work_and_totals(self):
        for _ in range(3):
            self.ingest()
        config = dataclasses.replace(self.config, batch_records=1)
        Exporter(self.store, config, FakeTransport()).run_once()
        Exporter(self.store, config, FakeTransport(Result("failed", "http_permanent", 400))).run_once()
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET delivered_at='2000-01-01',failed_at='2000-01-01'")
        self.store.cleanup(config)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.store.status()["delivered_events_total"], 1)
        self.assertEqual(self.store.status()["failed_events_total"], 1)
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET created_at='2000-01-01'")
        self.assertEqual(self.store.claim(config), [])
        self.assertEqual(self.store.status()["dropped_events_total"], 4)

    def test_capacity_protects_sending_and_lease_renewal(self):
        self.db.otlp_max_pending = 1
        self.ingest()
        rows = self.store.claim(self.config)
        self.ingest()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["status"], "sending")
        self.assertEqual(self.store.status()["dropped_events_total"], 3)
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET lease_expires_at=?", ((datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),))
        self.assertTrue(self.store.begin_attempt(rows, self.config))
        self.assertGreater((datetime.fromisoformat(self.rows()[0]['lease_expires_at']) - datetime.now(timezone.utc)).total_seconds(), 50)

    def test_busy_worker_waits_and_can_stop(self):
        exporter = Exporter(self.store, self.config, FakeTransport())
        with patch.object(self.store, 'claim', side_effect=sqlite3.OperationalError('database is locked')) as claim:
            with patch.object(exporter.stop, 'wait', side_effect=lambda timeout: exporter.stop.set()) as wait:
                exporter.run()
        self.assertEqual(claim.call_count, 1)
        wait.assert_called_once_with(self.config.poll_interval)

    def test_retry_jitter_is_capped(self):
        self.ingest()
        with self.db.connect() as conn:
            conn.execute("UPDATE otlp_outbox SET attempt_count=10000")
        with patch('ai_asset_inventory.otlp_exporter.random.uniform', return_value=60) as jitter:
            Exporter(self.store, self.config, FakeTransport(Result('retry', 'transport'))).run_once()
        jitter.assert_called_once_with(0, 60)

    def test_sigterm_during_request_leaves_recoverable_claim(self):
        self.ingest()
        started = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                started.set()
                time.sleep(1)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = {**os.environ, **ENV, 'EDGEDISCO_OTLP_SHUTDOWN_GRACE_SECONDS': '0',
               'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT': f'http://127.0.0.1:{server.server_port}/v1/logs'}
        process = subprocess.Popen([sys.executable, '-m', 'ai_asset_inventory', 'otlp-export', '--db', str(self.path)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(started.wait(5))
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(self.rows()[0]['status'], 'sending')
            self.assertEqual(self.rows()[0]['attempt_count'], 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_shutdown_leaves_inflight_lease_and_no_new_claims(self):
        self.ingest()
        transport = FakeTransport(None)
        exporter = Exporter(self.store, self.config, transport)
        exporter.run_once()
        self.assertEqual(self.rows()[0]["status"], "sending")
        exporter.stop.set()
        self.assertEqual(exporter.run_once(), 0)

    def test_fail_closed_missing_and_future_database(self):
        missing = Path(self.temp.name) / "missing.db"
        with self.assertRaises(RuntimeError):
            OutboxStore(missing)
        self.assertFalse(missing.exists())
        with self.db.connect() as conn:
            conn.execute("PRAGMA user_version=999")
        with self.assertRaises(RuntimeError):
            OutboxStore(self.path)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_rollback_converts_inflight_rows_for_legacy_schema(self):
        from ai_asset_inventory.upgrade import _preserve_evidence
        self.ingest()
        self.store.claim(self.config)
        old_path = Path(self.temp.name) / 'old.db'
        old = Database(old_path)
        with old.connect() as conn:
            conn.executescript("""DROP TABLE otlp_outbox;
                CREATE TABLE otlp_outbox (
                    id TEXT PRIMARY KEY,asset_key TEXT NOT NULL,payload_json TEXT NOT NULL,payload_bytes INTEGER NOT NULL,
                    status TEXT CHECK(status IN ('pending','retry','delivered','failed')),attempt_count INTEGER DEFAULT 0,
                    next_attempt_at TEXT,created_at TEXT,delivered_at TEXT,last_error_code TEXT);
                PRAGMA user_version=2;""")
        _preserve_evidence(self.path, old_path)
        with old.connect() as conn:
            row = conn.execute('SELECT * FROM otlp_outbox WHERE id=?', (self.rows()[0]['id'],)).fetchone()
            self.assertEqual(row['status'], 'retry')
            self.assertEqual(row['payload_json'], self.rows()[0]['payload_json'])
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_legacy_migration_preserves_pending_events(self):
        self.ingest()
        original = self.rows()[0]
        with self.db.connect() as conn:
            conn.executescript("""DROP INDEX idx_otlp_outbox_due;
                ALTER TABLE otlp_outbox RENAME TO new_outbox;
                CREATE TABLE otlp_outbox (
                    id TEXT PRIMARY KEY,asset_key TEXT NOT NULL,payload_json TEXT NOT NULL,payload_bytes INTEGER NOT NULL,
                    status TEXT CHECK(status IN ('pending','retry','delivered','failed')),attempt_count INTEGER DEFAULT 0,
                    next_attempt_at TEXT,created_at TEXT,delivered_at TEXT,last_error_code TEXT);
                INSERT INTO otlp_outbox SELECT id,asset_key,payload_json,payload_bytes,status,attempt_count,next_attempt_at,created_at,delivered_at,last_error_code FROM new_outbox;
                DROP TABLE new_outbox; PRAGMA user_version=2;""")
        migrated = OutboxStore(self.path)
        Exporter(migrated, self.config, FakeTransport()).run_once()
        self.assertEqual(self.rows()[0]["payload_json"], original["payload_json"])
        self.assertEqual(self.rows()[0]["status"], "delivered")


@unittest.skipUnless(ExportLogsServiceRequest, "optional OTLP dependency not installed")
class ResponseTests(unittest.TestCase):
    def test_response_classes(self):
        partial = ExportLogsServiceResponse()
        partial.partial_success.rejected_log_records = 1
        partial.partial_success.error_message = "SECRET"
        for status, body, expected, code in (
            (200, b"", "delivered", None), (204, b"", "delivered", None),
            (200, partial.SerializeToString(), "failed", "partial_success"),
            (200, b"invalid", "retry", "invalid_response"),
            (200, b"x" * (MAX_RESPONSE_BYTES + 1), "retry", "invalid_response"),
            *((s, b"SECRET", "retry", "http_retryable") for s in (429, 502, 503, 504)),
            *((s, b"SECRET", "failed", "http_permanent") for s in (301, 400, 401, 403, 404, 500)),
        ):
            result = classify_response(status, body, "600")
            self.assertEqual((result.status, result.code), (expected, code))
            self.assertNotIn("SECRET", repr(result))
        self.assertEqual(retry_after_seconds("600"), 300)
        self.assertEqual(retry_after_seconds("nonsense"), 0)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("Thu, 01 Jan 2026 00:00:30 GMT", now), 30)

    def test_http_gzip_no_proxy_no_redirect_and_deadlines(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append((self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length'])), self.client_address))
                if self.path == "/slow":
                    time.sleep(0.4)
                if self.path == "/disconnect":
                    self.connection.close()
                    return
                self.send_response(302 if self.path == "/redirect" else 200)
                self.send_header("Content-Length", "0")
                self.send_header("Location", "/unexpected")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = dataclasses.replace(ExportConfig.from_env(ENV), endpoint=f"http://127.0.0.1:{server.server_port}/v1/logs", compression="gzip")
        transport = HttpTransport(config)
        self.addCleanup(transport.close)
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}):
            for _ in range(2):
                self.assertEqual(transport.send(b"test").status, "delivered")
        self.assertEqual(gzip.decompress(received[0][2]), b"test")
        self.assertEqual(received[0][1]["Content-Type"], "application/x-protobuf")
        self.assertEqual(received[0][3], received[1][3])  # reused connection
        for path, expected in (("redirect", "http_permanent"), ("disconnect", "transport"), ("slow", "transport")):
            transport = HttpTransport(dataclasses.replace(config, endpoint=config.endpoint.replace("v1/logs", path), timeout=0.08))
            self.addCleanup(transport.close)
            started = time.monotonic()
            self.assertEqual(transport.send(b"test").code, expected)
            self.assertLess(time.monotonic() - started, 0.3)
        stop = threading.Event()
        stop.set()
        transport = HttpTransport(dataclasses.replace(config, endpoint=config.endpoint.replace("v1/logs", "slow"), shutdown_grace=0))
        self.assertIsNone(transport.send(b"test", stop))
        self.assertNotIn("/unexpected", [r[0] for r in received])


@unittest.skipUnless(ExportLogsServiceRequest, "optional OTLP dependency not installed")
class TlsAndServiceTests(unittest.TestCase):
    def test_tls_trust_and_mutual_authentication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key, cert, conf = root / 'key.pem', root / 'cert.pem', root / 'openssl.cnf'
            conf.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n')
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-keyout', str(key), '-out', str(cert), '-config', str(conf)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key.chmod(0o600)
            cert.chmod(0o600)
            received = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_POST(self):
                    received.append(self.connection.getpeercert())
                    self.rfile.read(int(self.headers['Content-Length']))
                    self.send_response(200)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            context.load_verify_locations(cert)
            context.verify_mode = ssl.CERT_REQUIRED
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                env = {**ENV, 'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT': f'https://127.0.0.1:{server.server_port}/v1/logs'}
                for additions, expected in (({}, 'retry'),
                    ({'OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE': str(cert)}, 'retry'),
                    ({'OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE': str(cert),
                      'OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE': str(cert),
                      'OTEL_EXPORTER_OTLP_LOGS_CLIENT_KEY': str(key)}, 'delivered')):
                    transport = HttpTransport(ExportConfig.from_env({**env, **additions}))
                    try:
                        self.assertEqual(transport.send(b'').status, expected)
                    finally:
                        transport.close()
                self.assertEqual(len(received), 1)
                self.assertTrue(received[0])
            finally:
                server.shutdown()
                server.server_close()

    def test_managed_exporter_enable_disable_and_env_roundtrip(self):
        from ai_asset_inventory.self_service import (default_layout, configure_exporter_service,
            EXPORTER_LABEL, _atomic_write, ensure_credentials, _parse_env)
        import shlex
        for system in ('Darwin', 'Linux'):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                layout = default_layout(home / '.edgedisco', home)
                values = {**ENV, 'OTEL_EXPORTER_OTLP_LOGS_HEADERS': "Authorization=Bearer SECRET'quoted"}
                _atomic_write(layout.env, ''.join(f'export {key}={shlex.quote(value)}\n' for key, value in values.items()))
                ensure_credentials(layout)
                self.assertEqual(_parse_env(layout.env)['OTEL_EXPORTER_OTLP_LOGS_HEADERS'], values['OTEL_EXPORTER_OTLP_LOGS_HEADERS'])
                service = (layout.launch_agents / f'{EXPORTER_LABEL}.plist' if system == 'Darwin' else
                           layout.systemd_user / f'{EXPORTER_LABEL}.service')
                with patch('ai_asset_inventory.self_service.platform.system', return_value=system), \
                     patch('ai_asset_inventory.self_service._restart_service'), \
                     patch('ai_asset_inventory.self_service._restart_systemd_service'), \
                     patch('ai_asset_inventory.self_service._launchctl', side_effect=[
                         subprocess.CompletedProcess([], 0, '', ''),
                         subprocess.CompletedProcess([], 113, '', 'Could not find service')]), \
                     patch('ai_asset_inventory.self_service._systemctl', return_value=
                         subprocess.CompletedProcess([], 0, 'inactive\n', '')):
                    configure_exporter_service(layout, values)
                    self.assertTrue(service.exists())
                    self.assertNotIn('SECRET', service.read_text())
                    self.assertIn('otlp-export --db', (layout.bin / 'run-otlp-export.sh').read_text())
                    configure_exporter_service(layout, {})
                    self.assertFalse(service.exists())

    def test_failed_or_unverified_stop_preserves_service_definition(self):
        from ai_asset_inventory.self_service import default_layout, configure_exporter_service, EXPORTER_LABEL
        for system in ('Darwin', 'Linux'):
            for stop_failed in (True, False):
                with self.subTest(system=system, stop_failed=stop_failed), tempfile.TemporaryDirectory() as temp:
                    layout = default_layout(Path(temp) / '.edgedisco', Path(temp))
                    service = (layout.launch_agents / f'{EXPORTER_LABEL}.plist' if system == 'Darwin'
                               else layout.systemd_user / f'{EXPORTER_LABEL}.service')
                    service.parent.mkdir(parents=True)
                    service.write_text('retain service definition')
                    results = [subprocess.CompletedProcess([], 1 if stop_failed else 0, '', 'SECRET failure'),
                               subprocess.CompletedProcess([], 0, 'active\n', '')]
                    tool = '_launchctl' if system == 'Darwin' else '_systemctl'
                    with patch('ai_asset_inventory.self_service.platform.system', return_value=system), \
                         patch('ai_asset_inventory.self_service.' + tool, side_effect=results):
                        with self.assertRaises(RuntimeError) as exc:
                            configure_exporter_service(layout, {})
                    self.assertNotIn('SECRET', str(exc.exception))
                    self.assertEqual(service.read_text(), 'retain service definition')

    def test_already_unloaded_macos_service_can_be_removed(self):
        from ai_asset_inventory.self_service import default_layout, configure_exporter_service, EXPORTER_LABEL
        with tempfile.TemporaryDirectory() as temp:
            layout = default_layout(Path(temp) / '.edgedisco', Path(temp))
            service = layout.launch_agents / f'{EXPORTER_LABEL}.plist'
            service.parent.mkdir(parents=True)
            service.write_text('unloaded service')
            with patch('ai_asset_inventory.self_service.platform.system', return_value='Darwin'), \
                 patch('ai_asset_inventory.self_service._launchctl', side_effect=[
                     subprocess.CompletedProcess([], 3, '', 'Boot-out failed: 3: No such process'),
                     subprocess.CompletedProcess([], 113, '', 'Could not find service')]):
                configure_exporter_service(layout, {})
            self.assertFalse(service.exists())


class StatusConfigurationTests(unittest.TestCase):
    def test_managed_status_and_explicit_file_ignore_ambient_configuration(self):
        from ai_asset_inventory.cli import main
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / '.edgedisco'
            db = Database(root / 'data/inventory.db')
            env_file = root / 'server.env'
            env_file.write_text(''.join(f'export {k}={v}\n' for k, v in ENV.items()))
            for options, ambient, expected, source in (
                ([], {}, True, 'file'),
                (['--env-file', str(env_file)], {}, True, 'file'),
                (['--process-env'], {}, False, 'environment'),
            ):
                output = io.StringIO()
                with patch('pathlib.Path.home', return_value=home), patch.dict(os.environ, ambient, clear=True), \
                     patch('sys.argv', ['edgedisco', 'otlp-status', '--db', str(db.path), '--json', *options]), \
                     contextlib.redirect_stdout(output):
                    main()
                status = json.loads(output.getvalue())
                self.assertEqual(status['export_enabled'], expected)
                self.assertEqual(status['configuration_source'], source)
            env_file.write_text('export EDGEDISCO_OTLP_EXPORT_ENABLED=false\n')
            output = io.StringIO()
            with patch('pathlib.Path.home', return_value=home), patch.dict(os.environ, ENV, clear=True), \
                 patch('sys.argv', ['edgedisco', 'otlp-status', '--json']), contextlib.redirect_stdout(output):
                main()
            self.assertFalse(json.loads(output.getvalue())['export_enabled'])

    def test_explicit_missing_env_file_fails_before_database_access(self):
        from ai_asset_inventory.cli import main
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'missing'
            with patch('sys.argv', ['edgedisco', 'otlp-status', '--env-file', str(missing), '--db', str(missing)]):
                with self.assertRaisesRegex(SystemExit, 'configuration file'):
                    main()
            self.assertFalse(missing.exists())

    def test_disable_happens_before_setup_server_failure(self):
        from ai_asset_inventory.self_service import default_layout, _setup_self_service
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            layout = default_layout(Path(temp) / '.edgedisco', Path(temp))
            restart = Mock(side_effect=RuntimeError('server failed'))
            with patch('ai_asset_inventory.self_service.configure_exporter_service') as configure, \
                 patch('ai_asset_inventory.self_service._health', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'server failed'):
                    _setup_self_service(layout, None, False, False, write_services=Mock(),
                                        restart_server=restart, restart_agent=Mock(), home=Path(temp))
                configure.assert_called_once()
                self.assertNotEqual(configure.call_args.args[1].get('EDGEDISCO_OTLP_EXPORT_ENABLED'), 'true')


if __name__ == '__main__':
    unittest.main()
