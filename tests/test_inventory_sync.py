import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.database import Database
from ai_asset_inventory.inventory_sync import bootstrap, changes, snapshot


def asset(name="CrewAI", running=True, fingerprint=None):
    return {"fingerprint": fingerprint or name, "kind": "agent_runtime", "name": name,
            "vendor": "Microsoft" if name == "AutoGen" else name, "running": running,
            "metadata": {"host_app": "Cursor", "relationship": "spawned_by",
                         "prompt": "PRIVATE_PROMPT", "api_key": "PRIVATE_KEY"}}


class InventorySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "inventory.db")
        self.device_id, _ = self.db.enroll({"hostname": "PRIVATE_HOST", "os": "Darwin"})
        self.number = 0

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, assets):
        self.number += 1
        self.db.ingest(self.device_id, {"scan_id": str(self.number),
            "observed_at": "2026-09-20T18:00:00+00:00", "assets": assets})

    def test_snapshot_and_cursor_are_stable_across_scans(self):
        self.scan([asset("CrewAI"), asset("AutoGen")])
        with self.db.connect() as conn:
            page1 = snapshot(conn, limit=1)
        self.assertEqual(len(page1["items"]), 1)
        self.assertIsNotNone(page1["next_after"])
        self.assertNotIn("PRIVATE_", json.dumps(page1))
        watermark = page1["watermark"]
        self.scan([asset("CrewAI", running=False)])
        with self.db.connect() as conn:
            page2 = snapshot(conn, watermark=watermark,
                             after=page1["next_after"], limit=1)
            old = snapshot(conn, watermark=watermark)
            current = snapshot(conn)
            delta = changes(conn, cursor=watermark)
        self.assertEqual(len(page2["items"]), 1)
        self.assertEqual(len(old["items"]), 2)
        self.assertEqual(len(current["items"]), 1)
        self.assertEqual({x["operation"] for x in delta["items"]}, {"upsert", "delete"})
        self.assertNotIn("PRIVATE_", json.dumps(delta))

    def test_repeated_state_does_not_emit_duplicate_and_cursor_pages(self):
        self.scan([asset()])
        with self.db.connect() as conn:
            first = changes(conn)
        self.scan([asset()])
        with self.db.connect() as conn:
            again = changes(conn, cursor=first["next_cursor"])
        self.assertEqual(again["items"], [])
        self.scan([])
        with self.db.connect() as conn:
            deleted = changes(conn, cursor=first["next_cursor"])
        self.assertEqual(deleted["items"][0]["operation"], "delete")

    def test_existing_database_is_seeded_for_first_consumer(self):
        self.scan([asset()])
        with self.db.connect() as conn:
            conn.execute("DELETE FROM inventory_sync_changes")
            bootstrap(conn)
            result = snapshot(conn)
        self.assertEqual(len(result["items"]), 1)

    def test_bootstrap_does_not_resurrect_asset_absent_from_latest_scan(self):
        self.scan([asset()])
        self.scan([])
        with self.db.connect() as conn:
            conn.execute("DELETE FROM inventory_sync_changes")
            bootstrap(conn)
            result = snapshot(conn)
        self.assertEqual(result["items"], [])

    def test_duplicate_logical_asset_prefers_running_projection(self):
        self.scan([
            asset(running=False, fingerprint="stopped-observation"),
            asset(running=True, fingerprint="running-observation"),
        ])
        with self.db.connect() as conn:
            result = snapshot(conn)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["attributes"]["asset.running"])
        self.scan([
            asset(running=True, fingerprint="running-observation"),
            asset(running=False, fingerprint="stopped-observation"),
        ])
        with self.db.connect() as conn:
            self.assertEqual(changes(conn, cursor=result["watermark"])["items"], [])

    def test_upgrade_seeds_all_devices_before_first_new_upload(self):
        self.scan([asset()])
        other, _ = self.db.enroll({"hostname": "other", "os": "Darwin"})
        with self.db.connect() as conn:
            conn.execute("DROP TABLE inventory_sync_changes")
        upgraded = Database(self.db.path)
        upgraded.ingest(other, {"scan_id": "other-scan",
            "observed_at": "2026-09-20T19:00:00+00:00", "assets": [asset()]})
        with upgraded.connect() as conn:
            result = snapshot(conn)
        self.assertEqual({item["attributes"]["device.id"] for item in result["items"]},
                         {self.device_id, other})

    def test_bootstrap_preserves_current_stopped_asset_only(self):
        self.scan([asset(), asset("AutoGen")])
        self.db.ingest(self.device_id, {"scan_id": "later",
            "observed_at": "2026-09-20T19:00:00+00:00",
            "assets": [asset("AutoGen", running=False)]})
        with self.db.connect() as conn:
            conn.execute("DELETE FROM inventory_sync_changes")
            bootstrap(conn)
            result = snapshot(conn)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["attributes"]["asset.name"], "AutoGen")
        self.assertFalse(result["items"][0]["attributes"]["asset.running"])


if __name__ == "__main__":
    unittest.main()
