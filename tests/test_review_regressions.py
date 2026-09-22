import copy
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.agent import AgentClient
from ai_asset_inventory.database import Database, utc_now
from ai_asset_inventory.runtime import append_event, normalize_hook_event, read_events
from ai_asset_inventory.server import RequestHandler


def asset(index=0):
    return {"fingerprint": f"{index:064x}", "kind": "process", "name": "Ollama",
            "vendor": "Ollama", "running": True, "metadata": {"executable": "ollama"}}


def report(scan, assets, observed_at=None):
    return {"scan_id": scan, "observed_at": observed_at or utc_now(),
            "device": {}, "privacy": {}, "assets": assets}


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "inventory.db", otlp_enabled=True)
        self.device, _ = self.db.enroll({"hostname": "test", "os": "Linux"})

    def test_future_observations_are_rejected_and_legacy_poison_does_not_block_scans(self):
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        poisoned = report("future", [asset()], future)
        with self.assertRaises(ValueError):
            RequestHandler._validate_report(poisoned)
        with self.assertRaises(ValueError):
            self.db.ingest(self.device, poisoned)
        self.db.ingest(self.device, report("legacy", [asset()]))
        with self.db.connect() as conn:
            conn.execute("UPDATE scans SET observed_at=? WHERE id='legacy'", (future,))
            conn.execute("UPDATE assets SET last_seen=?", (future,))
        self.db.ingest(self.device, report("recovered", []))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT running FROM assets").fetchone()[0], 0)
        self.db.ingest(self.device, report("current", [asset()]))
        self.assertEqual(self.db.summary()["running"], 1)

    def test_large_spool_retries_without_losing_events_or_new_writes(self):
        config = self.root / "agent.json"
        config.write_text(json.dumps({"device_token": "test"}))
        path = self.root / "runtime-events.jsonl"
        for i in range(1001):
            append_event(path, normalize_hook_event("sdk", "sessionStart", {"session_id": str(i)}))
        client = AgentClient(config)
        calls = 0
        def upload(route, body, token):
            nonlocal calls
            calls += 1
            if calls == 2:
                append_event(path, normalize_hook_event("sdk", "sessionEnd", {"session_id": "new"}))
                raise RuntimeError("network interrupted")
            self.db.ingest_runtime_events(self.device, body["events"])
            return {}
        with patch.object(client, "_request", side_effect=upload):
            with self.assertRaisesRegex(RuntimeError, "network interrupted"):
                client.flush_runtime_events()
            self.assertTrue(path.with_suffix(".jsonl.pending").exists())
            self.assertEqual(client.flush_runtime_events(), 1001)
            self.assertEqual(len(read_events(path)), 1)
            self.assertEqual(client.flush_runtime_events(), 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0], 1002)

    def test_inventory_rejects_content_and_bad_shapes(self):
        valid = report("valid", [asset()])
        RequestHandler._validate_report(valid)
        for field, value in (("metadata", {"prompt": "private"}),
                             ("path_hash", "/private/path"), ("command_hash", "raw command"),
                             ("binary_sha256", "not-a-hash"),
                             ("version", {"secret": "private"}), ("credentials", "secret")):
            invalid = copy.deepcopy(valid)
            invalid["assets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                RequestHandler._validate_report(invalid)
        for invalid in (None, [], {**valid, "observed_at": "yesterday"},
                        {**valid, "privacy": {"prompt": "private"}},
                        {**valid, "device": {"token": "private"}}):
            with self.assertRaises(ValueError):
                RequestHandler._validate_report(invalid)
        version_two = copy.deepcopy(valid)
        version_two["schema_version"] = 2
        version_two["privacy"] = {
            "content_captured": False, "secrets_captured": False,
            "paths_hashed": True, "command_lines_hashed": True,
            "binary_contents_hashed": True,
        }
        version_two["assets"][0].update({
            "binary_sha256": "a" * 64,
            "binary_fingerprint_status": "unlisted",
            "fingerprint_library_version": "2026-09-20",
        })
        RequestHandler._validate_report(version_two)

    def test_late_scans_and_events_do_not_replace_newer_state(self):
        self.db.ingest(self.device, report("new", [asset()], "2026-09-20T02:00:00.000002+00:00"))
        self.db.ingest(self.device, report("old", [], "2026-09-20T02:00:00.000001+00:00"))
        self.assertTrue(self.db.summary()["items"][0]["stale"])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT running,present FROM assets").fetchone()[:], (1, 1))
        end = normalize_hook_event("sdk", "sessionEnd", {"session_id": "s"})
        end["observed_at"] = "2026-09-20T02:00:00+00:00"
        start = normalize_hook_event("sdk", "sessionStart", {"session_id": "s"})
        start["observed_at"] = "2026-09-20T02:30:00+01:00"
        self.db.ingest_runtime_events(self.device, [end, start])
        session = self.db.summary()["session_items"][0]
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["last_event"], "sessionEnd")
        self.assertLess(session["first_seen"], session["last_seen"])
        self.assertEqual(session["event_count"], 2)

    def test_exports_include_all_assets_and_sessions(self):
        self.db.ingest(self.device, report("large", [asset(i) for i in range(501)]))
        events = [normalize_hook_event("sdk", "sessionStart", {"session_id": str(i)}) for i in range(501)]
        self.db.ingest_runtime_events(self.device, events)
        for exported in (
            self.db.export_csv(), self.db.export_agent_sessions_csv(),
            self.db.export_runtime_events_csv(),
        ):
            self.assertEqual(len(list(csv.DictReader(io.StringIO(exported)))), 501)

    def test_exports_include_complete_allowlisted_evidence(self):
        evidence = asset()
        evidence.update({
            "path_hash": "a" * 64,
            "command_hash": "b" * 64,
            "binary_sha256": "c" * 64,
            "binary_fingerprint_status": "unlisted",
            "fingerprint_library_version": "2026-09-20",
            "metadata": {
                "discovery_source": "Process snapshot", "executable": "ollama",
                "package": "ollama-package", "configured_in": "settings.json",
                "transport": "stdio", "host_app": "Cursor", "runtime": "python",
                "relationship": "spawned_by", "instance_count": 2,
                "demo_lab": True, "evidence_label": "SIMULATED TEST WORKLOADS",
                "observed_running": True,
            },
        })
        self.db.ingest(self.device, report("complete", [evidence]))
        event = normalize_hook_event("sdk", "postToolUse", {
            "session_id": "session", "agent_id": "agent", "workspace": "/private/project",
            "user_id": "person", "tool_name": "MCP:search/query", "model": "model",
            "source": "test-sdk", "duration_ms": 42,
        })
        self.db.ingest_runtime_events(self.device, [event])

        assets = csv.DictReader(io.StringIO(self.db.export_csv()))
        self.assertEqual(assets.fieldnames, [
            "device_id", "hostname", "os", "os_version", "machine", "agent_version",
            "fingerprint", "kind", "name", "vendor", "version", "discovery_source",
            "executable", "package", "configured_in", "transport", "host_app", "runtime",
            "relationship", "instance_count", "demo_lab", "evidence_label", "observed_running",
            "path_hash", "command_hash", "binary_sha256", "binary_fingerprint_status",
            "fingerprint_library_version", "status", "running", "present", "first_seen", "last_seen", "stale",
        ])
        asset_row = next(assets)
        self.assertEqual(asset_row["fingerprint"], evidence["fingerprint"])
        self.assertEqual(asset_row["path_hash"], "a" * 64)
        self.assertEqual(asset_row["discovery_source"], "Process snapshot")
        self.assertEqual(asset_row["instance_count"], "2")
        self.assertEqual(asset_row["status"], "Running")

        sessions = csv.DictReader(io.StringIO(self.db.export_agent_sessions_csv()))
        self.assertEqual(sessions.fieldnames, [
            "device_id", "hostname", "os", "os_version", "machine", "agent_version",
            "session_hash", "agent_hash", "app", "agent_type", "model", "status",
            "workspace_hash", "user_hash", "first_seen", "last_seen", "last_event",
            "event_count", "tool_count", "mcp_count", "duration_ms",
        ])
        session_row = next(sessions)
        for key in ("session_hash", "agent_hash", "workspace_hash", "user_hash"):
            self.assertEqual(session_row[key], event[key])

        runtime_events = csv.DictReader(io.StringIO(self.db.export_runtime_events_csv()))
        self.assertEqual(runtime_events.fieldnames, [
            "event_id", "device_id", "hostname", "os", "os_version", "machine", "agent_version",
            "observed_at", "received_at", "app", "event_type", "session_hash", "agent_hash",
            "agent_type", "tool_name", "mcp_server", "model", "status", "duration_ms",
            "workspace_hash", "user_hash", "source_event", "cursor_version", "permission_mode", "source",
        ])
        event_row = next(runtime_events)
        self.assertEqual(event_row["event_id"], event["event_id"])
        self.assertEqual(event_row["mcp_server"], "search")
        self.assertEqual(event_row["source"], "test-sdk")

    def test_csv_exports_neutralize_spreadsheet_formulas(self):
        self.db.ingest(self.device, report("formula", [asset()]))
        event = normalize_hook_event("sdk", "sessionStart", {"session_id": "formula", "model": "@MODEL"})
        self.db.ingest_runtime_events(self.device, [event])
        with self.db.connect() as conn:
            conn.execute("UPDATE devices SET hostname='=HYPERLINK(\"https://example.invalid\")'")
            conn.execute("UPDATE assets SET version='  +1+1'")
        for exported in (
            self.db.export_csv(), self.db.export_agent_sessions_csv(),
            self.db.export_runtime_events_csv(),
        ):
            row = next(csv.DictReader(io.StringIO(exported)))
            self.assertTrue(row["hostname"].startswith("'="))
        asset_row = next(csv.DictReader(io.StringIO(self.db.export_csv())))
        event_row = next(csv.DictReader(io.StringIO(self.db.export_runtime_events_csv())))
        self.assertEqual(asset_row["version"], "'  +1+1")
        self.assertEqual(asset_row["discovery_source"], "Process snapshot")
        self.assertEqual(event_row["model"], "'@MODEL")

    def test_stale_inventory_is_not_running_even_with_runtime_upload(self):
        self.db.ingest(self.device, report("scan", [asset()]))
        with self.db.connect() as conn:
            conn.execute("UPDATE scans SET received_at='2000-01-01T00:00:00+00:00'")
        self.db.ingest_runtime_events(self.device, [normalize_hook_event("sdk", "sessionStart", {"session_id": "s"})])
        summary = self.db.summary()
        self.assertEqual(summary["running"], 0)
        self.assertTrue(summary["items"][0]["stale"])
        self.assertEqual(self.db.list_assets(running_only=True), [])

    def test_disappearance_updates_otlp_and_restart_emits_running(self):
        for scan, assets, expected in (("running", [asset()], True), ("stopped", [], False), ("restart", [asset()], True)):
            self.db.ingest(self.device, report(scan, assets))
            with self.db.connect() as conn:
                rows = conn.execute("SELECT payload_json FROM otlp_outbox").fetchall()
                rows = [r for r in rows if json.loads(r[0])["event.name"] == "edgedisco.asset.observed"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0][0])["attributes"]["asset.running"], expected)
