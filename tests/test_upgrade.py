import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.configuration import load_config, migrate_config
from ai_asset_inventory.database import Database, DATABASE_VERSION
from ai_asset_inventory.self_service import default_layout, setup_macos
from ai_asset_inventory.upgrade import snapshot, restore
from ai_asset_inventory.adapters import install_cursor, install_claude, install_copilot


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".edgedisco"
        self.root.mkdir()

    def test_config_migration_preserves_custom_settings_and_is_idempotent(self):
        config = {"scan_interval_seconds": 900, "runtime_spool": "/custom/events.jsonl",
                  "ca_file": "/custom/ca.pem", "custom_setting": {"keep": True}}
        path = self.root / "agent.json"
        path.write_text(json.dumps(config))
        migrated = migrate_config(load_config(path), server_url="http://127.0.0.1:9000", spool=self.root / "events")
        for key, value in config.items():
            self.assertEqual(migrated[key], value)
        self.assertEqual(migrated["config_version"], 1)
        self.assertEqual(migrate_config(migrated, server_url=migrated["server_url"], spool=self.root / "other"), migrated)

    def test_changed_hook_paths_replace_managed_commands(self):
        for installer in (install_cursor, install_claude, install_copilot):
            target = self.home / (installer.__name__ + ".json")
            installer(self.root / "old-config.json", target)
            before = json.loads(target.read_text())
            first_event = next(iter(before["hooks"]))
            before["hooks"][first_event].append({"command": "echo user-hook"})
            target.write_text(json.dumps(before))
            installer(self.root / "new-config.json", target)
            once = target.read_text()
            installer(self.root / "new-config.json", target)
            self.assertEqual(target.read_text(), once)
            self.assertNotIn("old-config.json", once)
            self.assertIn("new-config.json", once)
            self.assertIn("echo user-hook", once)

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service.ensure_credentials")
    def test_bad_or_future_config_fails_before_setup_changes(self, credentials, system):
        path = self.root / "agent.json"
        for text in ("broken-json", "[]", '{"config_version": 999}'):
            path.write_text(text)
            with self.assertRaises(RuntimeError):
                setup_macos(root=self.root, home=self.home, open_dashboard=False)
            self.assertEqual(path.read_text(), text)
            credentials.assert_not_called()
            with self.assertRaises(RuntimeError):
                snapshot(self.root, self.home)
            self.assertFalse((self.root / "backups").exists())

    def test_database_migration_is_versioned_and_refuses_future_versions(self):
        path = self.root / "inventory.db"
        db = Database(path)
        device, token = db.enroll({"hostname": "keep", "os": "Linux"})
        with db.connect() as conn:
            conn.execute("PRAGMA user_version=0")
            conn.execute("DROP TABLE otlp_asset_state")
        upgraded = Database(path)
        self.assertEqual(upgraded.device_for_token(token)["id"], device)
        with upgraded.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], DATABASE_VERSION)
            conn.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Database(path)

    def test_failed_migration_rolls_back_ddl_and_version(self):
        path = self.root / "broken.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE assets(wrong_column TEXT)")
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            Database(path)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='devices'").fetchone())
        conn.close()

    @patch("ai_asset_inventory.upgrade.start_services")
    @patch("ai_asset_inventory.upgrade.stop_services")
    def test_snapshot_restores_package_config_and_database_without_losing_spool(self, stop, start):
        config = self.root / "agent.json"
        config.write_text(json.dumps({"device_token": "old-token", "custom": True}))
        package = self.root / "venv" / "package"
        package.parent.mkdir()
        package.write_text("old package")
        db = Database(self.root / "data/inventory.db")
        _, token = db.enroll({"hostname": "old", "os": "Linux"})
        hook = self.home / ".cursor/hooks.json"
        hook.parent.mkdir()
        hook.write_text('{"hooks": {"custom": []}}')
        backup = snapshot(self.root, self.home)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        package.write_text("broken package")
        config.write_text('{"custom": false}')
        hook.write_text("broken hook")
        db.enroll({"hostname": "failed-attempt", "os": "Linux"})
        spool = self.root / "runtime-events.jsonl"
        spool.write_text("new events must survive")
        restore(backup)
        self.assertEqual(package.read_text(), "old package")
        self.assertEqual(json.loads(config.read_text())["device_token"], "old-token")
        self.assertIn("custom", hook.read_text())
        self.assertEqual(spool.read_text(), "new events must survive")
        self.assertIsNotNone(Database(db.path).device_for_token(token))
        self.assertEqual(Database(db.path).summary()["devices"], 1)
        self.assertEqual(Database(backup / "failed-state/data/inventory.db").summary()["devices"], 2)
        stop.assert_called_once_with(self.root.resolve())
        start.assert_called_once_with(self.home.resolve())
        restore(backup)
        self.assertEqual(package.read_text(), "old package")
        self.assertEqual(len(list(backup.glob("failed-state*"))), 2)
