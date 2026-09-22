import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_asset_inventory.database import Database
from ai_asset_inventory.otlp_store import OutboxStore


CONFIG = SimpleNamespace(
    batch_records=100,
    batch_bytes=1_048_576,
    lease_seconds=60,
    timeout=10,
    shutdown_grace=15,
    delivered_days=1,
    failed_days=1,
)


def report(scan_id="scan-1", running=True):
    return {
        "scan_id": scan_id,
        "observed_at": "2026-09-21T12:00:00+00:00",
        "privacy": {"content_captured": False},
        "assets": [{
            "fingerprint": "fixture-runtime",
            "kind": "agent_runtime",
            "name": "Ollama",
            "vendor": "Ollama",
            "running": running,
            "present": True,
            "metadata": {"host_app": "Cursor", "relationship": "spawned_by"},
        }],
    }


class OutboxInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inventory.db"
        self.db = Database(self.path, otlp_enabled=True)
        self.device_id, _ = self.db.enroll({"hostname": "fixture-host", "os": "Darwin"})
        self.store = OutboxStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def _pending_rows(self):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT * FROM otlp_outbox WHERE status IN ('pending', 'retry')"
            ).fetchall()

    def test_retry_keeps_event_durable_and_claimable(self):
        self.db.ingest(self.device_id, report())

        claimed = self.store.claim(CONFIG)
        self.assertEqual(len(claimed), 2)
        self.assertTrue(self.store.begin_attempt(claimed, CONFIG))
        self.assertEqual(
            self.store.finish(claimed, "retry", code="transport", delay=0),
            2,
        )

        rows = self._pending_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["attempt_count"] == 1 for row in rows))
        self.assertTrue(all(row["last_error_code"] == "transport" for row in rows))

        reclaimed = self.store.claim(CONFIG)
        self.assertEqual({row["id"] for row in reclaimed}, {row["id"] for row in rows})

    def test_expired_lease_is_retried_without_duplicate_event(self):
        self.db.ingest(self.device_id, report())
        claimed = self.store.claim(CONFIG)
        self.assertEqual(len(claimed), 2)

        with self.store.connect() as conn:
            conn.execute(
                "UPDATE otlp_outbox SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
                (claimed[0]["id"], claimed[1]["id"]),
            )

        reclaimed = self.store.claim(CONFIG)
        self.assertEqual({row["id"] for row in reclaimed}, {row["id"] for row in claimed})
        self.assertEqual(self.store.status()["retried_events_total"], 2)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM otlp_outbox").fetchone()[0], 2)

    def test_newer_state_supersedes_retry_without_replaying_old_payload(self):
        self.db.ingest(self.device_id, report("old", running=True))
        first = self.store.claim(CONFIG)
        self.assertTrue(self.store.begin_attempt(first, CONFIG))
        self.assertEqual(self.store.finish(first, "retry", code="transport"), 2)

        self.db.ingest(self.device_id, report("new", running=False))
        claimed = self.store.claim(CONFIG)
        asset_rows = [
            row for row in claimed
            if json.loads(row["payload_json"])["event.name"] == "edgedisco.asset.observed"
        ]
        self.assertEqual(len(asset_rows), 1)
        payload = json.loads(asset_rows[0]["payload_json"])
        self.assertFalse(payload["attributes"]["asset.running"])
        self.assertNotEqual(asset_rows[0]["id"], first[0]["id"])


if __name__ == "__main__":
    unittest.main()
