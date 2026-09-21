import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.database import Database
from ai_asset_inventory.inventory_sync import bootstrap, changes, snapshot


def asset(name="CrewAI", running=True):
    return {"fingerprint": name, "kind": "agent_runtime", "name": name,
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


if __name__ == "__main__":
    unittest.main()
