import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_asset_inventory.database import Database


SENTINELS = (
    "EDGEDISCO_SECRET_SENTINEL", "EDGEDISCO_PROMPT_SENTINEL",
    "EDGEDISCO_RESPONSE_SENTINEL", "EDGEDISCO_COMMAND_SENTINEL",
    "EDGEDISCO_SOURCE_SENTINEL", "EDGEDISCO_PATH_SENTINEL",
    "EDGEDISCO_METADATA_SENTINEL",
)


def asset(*, running=True, simulated=False, name="CrewAI", fingerprint="runtime-1", version=None):
    metadata = {
        "host_app": "Cursor", "relationship": "spawned_by",
        "prompt": SENTINELS[1], "response": SENTINELS[2],
        "api_token": SENTINELS[0], "raw_command_line": SENTINELS[3],
        "source_code": SENTINELS[4], "filesystem_path": SENTINELS[5],
        "nested": {"arbitrary": SENTINELS[6]},
    }
    if simulated:
        metadata.update(demo_lab=True, evidence_label="SIMULATED TEST WORKLOADS")
    return {
        "fingerprint": fingerprint, "kind": "agent_runtime", "name": name,
        "vendor": "CrewAI", "version": version, "running": running,
        "path_hash": SENTINELS[5], "command_hash": SENTINELS[3],
        "metadata": metadata, "credentials": SENTINELS[0],
        "prompt": SENTINELS[1], "response": SENTINELS[2],
        "source_code": SENTINELS[4],
    }


def report(scan_id, assets):
    return {
        "scan_id": scan_id, "observed_at": "2026-09-20T18:00:00+00:00",
        "privacy": {"content_captured": False, "secret": SENTINELS[0]},
        "device": {"prompt": SENTINELS[1]}, "assets": assets,
    }


class OtlpOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inventory.db"
        self.db = Database(self.path, otlp_enabled=True)
        self.device_id, _ = self.db.enroll({"hostname": "host", "os": "Darwin"})

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM otlp_outbox ORDER BY created_at,id").fetchall()

    def dropped(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM otlp_export_status WHERE id=1").fetchone()

    def test_only_allowlisted_fields_enter_actual_sqlite_payload(self):
        self.assertEqual(self.db.ingest(self.device_id, report("one", [asset()])), 1)
        row = self.rows()[0]
        payload = json.loads(row["payload_json"])
        self.assertEqual(set(payload), {
            "timestamp", "recorded_at", "event.name", "resource", "attributes",
        })
        self.assertEqual(payload["resource"], {"service.name": "edgedisco"})
        self.assertEqual(payload["event.name"], "edgedisco.asset.observed")
        self.assertEqual(set(payload["attributes"]), {
            "edgedisco.schema.version", "edgedisco.observation.id", "device.id",
            "asset.kind", "asset.name", "asset.vendor", "asset.running",
            "asset.host_app", "asset.relationship", "edgedisco.simulated",
        })
        self.assertFalse(payload["attributes"]["edgedisco.simulated"])
        self.assertEqual(payload["attributes"]["asset.name"], "CrewAI")
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, row["payload_json"])

    def test_demo_evidence_is_labeled_and_cannot_be_mislabeled(self):
        self.db.ingest(self.device_id, report("demo", [asset(running=False, simulated=True)]))
        self.assertTrue(json.loads(self.rows()[0]["payload_json"])["attributes"]["edgedisco.simulated"])
        unsafe = asset(fingerprint="unsafe")
        unsafe["metadata"]["evidence_label"] = "SIMULATED TEST WORKLOADS"
        self.db.ingest(self.device_id, report("conflict", [unsafe]))
        self.assertEqual(len(self.rows()), 1)

    def test_unrecognized_or_configuration_supplied_names_do_not_export(self):
        unknown = asset(name="EDGEDISCO_SECRET_SENTINEL", fingerprint="unknown")
        unknown["vendor"] = "EDGEDISCO_SECRET_SENTINEL"
        mcp = {"fingerprint": "mcp", "kind": "mcp_server", "name": SENTINELS[0],
               "vendor": "Unknown", "running": False,
               "metadata": {"command": SENTINELS[3]}}
        self.assertEqual(self.db.ingest(self.device_id, report("names", [unknown, mcp])), 2)
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(self.db.summary()["assets"], 2)

    def test_repeated_scans_keep_one_event_and_state_change_gets_new_id(self):
        self.db.ingest(self.device_id, report("one", [asset()]))
        first = self.rows()[0]["id"]
        self.db.ingest(self.device_id, report("two", [asset(fingerprint="changed-raw-fingerprint")]))
        self.assertEqual([row["id"] for row in self.rows()], [first])
        self.db.ingest(self.device_id, report("three", [asset(running=False)]))
        rows = self.rows()
        self.assertEqual(len(rows), 1)  # The older pending state was superseded.
        self.assertNotEqual(rows[0]["id"], first)
        self.assertEqual(self.dropped()["dropped_events_total"], 1)
        self.db.ingest(self.device_id, report("four", [asset(running=False)]))
        self.assertEqual(self.rows()[0]["id"], rows[0]["id"])

    def test_version_change_is_an_exported_state_change(self):
        self.db.ingest(self.device_id, report("version-one", [asset(version="1.0")]))
        first = self.rows()[0]["id"]
        self.db.ingest(self.device_id, report("version-two", [asset(version="2.0")]))
        row = self.rows()[0]
        self.assertNotEqual(row["id"], first)
        self.assertEqual(json.loads(row["payload_json"])["attributes"]["asset.version"], "2.0")

    def test_capacity_and_age_drop_only_outbound_evidence(self):
        self.db = Database(self.path, otlp_enabled=True, otlp_max_pending=1, otlp_max_age_days=1)
        self.db.ingest(self.device_id, report("one", [asset()]))
        first = self.rows()[0]["id"]
        self.db.ingest(self.device_id, report("two", [asset(simulated=True)]))
        self.assertEqual(len(self.rows()), 1)
        self.assertNotEqual(self.rows()[0]["id"], first)
        self.assertEqual(self.dropped()["last_drop_reason"], "capacity")
        self.assertTrue(self.db.otlp_outbox_status()["degraded"])
        self.assertEqual(self.db.otlp_outbox_status()["dropped_events_total"], 1)
        self.assertEqual(self.db.summary()["assets"], 1)
        with self.db.connect() as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            conn.execute("UPDATE otlp_outbox SET created_at=?", (old,))
        self.db.ingest(self.device_id, report("three", [asset(simulated=True)]))
        self.assertEqual(len(self.rows()), 1)
        self.assertNotEqual(self.rows()[0]["created_at"], old)
        self.assertEqual(self.dropped()["dropped_events_total"], 2)
        self.assertEqual(self.dropped()["last_drop_reason"], "age")
        self.db.ingest(self.device_id, report("four", [asset(simulated=True)]))
        self.assertEqual(len(self.rows()), 1)  # Eviction repaired dedup state.

    def test_disabled_outbox_and_existing_database_upgrade(self):
        self.db.ingest(self.device_id, report("one", [asset()]))
        with self.db.connect() as conn:
            conn.execute("DROP TABLE otlp_asset_state")
            conn.execute("DROP TABLE otlp_export_status")
            conn.execute("DROP TABLE otlp_outbox")
        upgraded = Database(self.path, otlp_enabled=False)
        self.assertEqual(upgraded.summary()["assets"], 1)
        upgraded.ingest(self.device_id, report("two", [asset(running=False)]))
        with upgraded.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM otlp_outbox").fetchone()[0], 0)
        self.assertEqual(upgraded.summary()["items"][0]["running"], False)

    def test_byte_bound_drops_outbound_record_without_losing_inventory(self):
        self.db = Database(self.path, otlp_enabled=True, otlp_max_payload_bytes=1)
        self.assertEqual(self.db.ingest(self.device_id, report("small-cap", [asset()])), 1)
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(self.dropped()["dropped_events_total"], 1)
        self.assertEqual(self.dropped()["last_drop_reason"], "oversize")
        self.assertEqual(self.db.summary()["assets"], 1)


if __name__ == "__main__":
    unittest.main()
