import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.configuration import load_config, migrate_config
from ai_asset_inventory.database import Database, DATABASE_VERSION, utc_now
from ai_asset_inventory.self_service import default_layout, setup_macos
from ai_asset_inventory.upgrade import main, snapshot, restore
from ai_asset_inventory.adapters import install_cursor, install_claude, install_copilot
from ai_asset_inventory.runtime import normalize_hook_event


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
        self.assertEqual(migrated["config_version"], 2)
        self.assertEqual(migrated["process_poll_interval_seconds"], 60)
        self.assertEqual(migrated["static_scan_interval_seconds"], 900)
        self.assertEqual(migrate_config(migrated, server_url=migrated["server_url"], spool=self.root / "other"), migrated)

    def test_incremental_scan_intervals_are_validated(self):
        path = self.root / "agent.json"
        for field, value in (
            ("process_poll_interval_seconds", 9),
            ("static_scan_interval_seconds", 59),
        ):
            with self.subTest(field=field):
                path.write_text(json.dumps({field: value}))
                with self.assertRaisesRegex(RuntimeError, field):
                    load_config(path)

    def test_version_one_config_gets_new_defaults_without_overwriting_custom_values(self):
        old = {"config_version": 1, "process_poll_interval_seconds": 30, "device_token": "keep"}
        result = migrate_config(old, server_url="http://127.0.0.1:8080", spool=self.root / "spool")
        self.assertEqual(result["config_version"], 2)
        self.assertEqual(result["process_poll_interval_seconds"], 30)
        self.assertEqual(result["static_scan_interval_seconds"], 900)
        self.assertEqual(result["device_token"], "keep")

    @patch("ai_asset_inventory.upgrade.start_services")
    @patch("ai_asset_inventory.upgrade.stop_services")
    def test_snapshot_quiesces_before_copy_and_restarts_on_failure(self, stop, start):
        (self.root / "agent.json").write_text('{}')
        def failed_copy(*args):
            stop.assert_called_once_with(self.root.resolve())
            raise OSError("disk full")
        with patch("ai_asset_inventory.upgrade._copy", side_effect=failed_copy):
            with self.assertRaisesRegex(OSError, "disk full"):
                snapshot(self.root, self.home, quiesce=True)
        start.assert_called_once_with(self.home.resolve())

    def test_unreadable_existing_database_fails_with_recovery_guidance(self):
        db = self.root / "data/inventory.db"
        db.parent.mkdir()
        db.touch()
        with patch("ai_asset_inventory.upgrade.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("unable to open database file")):
            with self.assertRaisesRegex(RuntimeError, "ownership and permissions"):
                snapshot(self.root, self.home)
        self.assertFalse((self.root / "backups").exists())

        error = io.StringIO()
        with patch.object(sys, "argv", ["upgrade", "snapshot", "--root", str(self.root)]), \
             patch("ai_asset_inventory.upgrade.snapshot",
                   side_effect=RuntimeError("Cannot open existing EdgeDisco database")), \
             redirect_stderr(error), self.assertRaises(SystemExit) as stopped:
            main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn("EdgeDisco upgrade error", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_existing_database_path_must_be_a_regular_file(self):
        db = self.root / "data/inventory.db"
        db.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "expected a regular file"):
            snapshot(self.root, self.home)

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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
            self.assertIn("binary_sha256", columns)
            self.assertIn("binary_fingerprint_status", columns)
            self.assertIn("fingerprint_library_version", columns)
            conn.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Database(path)

    def test_version_one_asset_rows_gain_binary_columns_without_data_loss(self):
        path = self.root / "legacy.db"
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE assets (
                    device_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL, name TEXT NOT NULL, vendor TEXT NOT NULL,
                    version TEXT, path_hash TEXT, command_hash TEXT, metadata_json TEXT NOT NULL,
                    running INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(device_id, fingerprint)
                );
                INSERT INTO assets VALUES(
                    'device', 'fingerprint', 'application', 'Existing', 'Vendor',
                    '1.0', 'path', NULL, '{}', 0, 'before', 'before'
                );
                PRAGMA user_version=1;
            """)
        Database(path)
        with sqlite3.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
            row = conn.execute(
                "SELECT name,version,binary_sha256,binary_fingerprint_status,"
                "fingerprint_library_version FROM assets"
            ).fetchone()
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], DATABASE_VERSION)
        self.assertEqual(
            columns & {"binary_sha256", "binary_fingerprint_status", "fingerprint_library_version"},
            {"binary_sha256", "binary_fingerprint_status", "fingerprint_library_version"},
        )
        self.assertEqual(row, ("Existing", "1.0", None, None, None))

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
    def test_rollback_keeps_new_evidence_without_upgrading_old_schema(self, stop, start):
        db = Database(self.root / 'data/inventory.db')
        device, _ = db.enroll({'hostname': 'old', 'os': 'Linux'})
        with db.connect() as conn:
            for column in ('binary_sha256', 'binary_fingerprint_status', 'fingerprint_library_version'):
                conn.execute(f'ALTER TABLE assets DROP COLUMN {column}')
            conn.execute('PRAGMA user_version=1')
        backup = snapshot(self.root, self.home)
        db = Database(db.path, otlp_enabled=True)
        db.ingest(device, {'scan_id':'new', 'observed_at':utc_now(), 'assets':[
            {'fingerprint':'a'*64, 'kind':'process', 'name':'Ollama', 'vendor':'Ollama',
             'running':True, 'metadata':{}}]})
        restore(backup)
        with db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM otlp_outbox').fetchone()[0], 1)
            self.assertNotIn('binary_sha256', {row[1] for row in conn.execute('PRAGMA table_info(assets)')})
    @patch("ai_asset_inventory.upgrade.start_services")
    @patch("ai_asset_inventory.upgrade.stop_services")
    def test_snapshot_restores_package_config_and_database_without_losing_spool(self, stop, start):
        config = self.root / "agent.json"
        config.write_text(json.dumps({"device_token": "old-token", "custom": True}))
        package = self.root / "venv" / "package"
        package.parent.mkdir()
        package.write_text("old package")
        db = Database(self.root / "data/inventory.db")
        device, token = db.enroll({"hostname": "old", "os": "Linux"})
        hook = self.home / ".cursor/hooks.json"
        hook.parent.mkdir()
        hook.write_text('{"hooks": {"custom": []}}')
        backup = snapshot(self.root, self.home)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        package.write_text("broken package")
        config.write_text('{"custom": false}')
        hook.write_text("broken hook")
        db.enroll({"hostname": "failed-attempt", "os": "Linux"})
        accepted = normalize_hook_event("sdk", "sessionStart", {"session_id": "accepted-during-upgrade"})
        db.ingest_runtime_events(device, [accepted])
        spool = self.root / "runtime-events.jsonl"
        spool.write_text("new events must survive")
        restore(backup)
        self.assertEqual(package.read_text(), "old package")
        self.assertEqual(json.loads(config.read_text())["device_token"], "old-token")
        self.assertIn("custom", hook.read_text())
        self.assertEqual(spool.read_text(), "new events must survive")
        self.assertIsNotNone(Database(db.path).device_for_token(token))
        self.assertEqual(Database(db.path).summary()["devices"], 2)
        with Database(db.path).connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runtime_events WHERE id=?", (accepted["event_id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0], 1)
        self.assertEqual(Database(backup / "failed-state/data/inventory.db").summary()["devices"], 2)
        stop.assert_called_once_with(self.root.resolve())
        start.assert_called_once_with(self.home.resolve())
        restore(backup)
        self.assertEqual(package.read_text(), "old package")
        self.assertEqual(len(list(backup.glob("failed-state*"))), 2)
