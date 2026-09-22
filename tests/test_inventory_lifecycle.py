"""Inventory-to-OTLP contract, including adversarial payloads and old schemas."""
import copy
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_asset_inventory.database import Database, utc_now
from ai_asset_inventory.otlp_encoder import _validated_event, encode_outbox_event
from ai_asset_inventory.otlp_events import DEVICE_EVENT_NAME, EVENT_NAME
from ai_asset_inventory.otlp_store import OutboxStore
from ai_asset_inventory.validation import validate_report


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inventory.db'
        self.db = Database(self.path, otlp_enabled=True)
        self.device, self.token = self.db.enroll({'hostname': 'PRIVATE_HOST', 'os': 'test'})
        self.asset = {'fingerprint': 'a' * 64, 'kind': 'application', 'name': 'Ollama',
                      'vendor': 'Ollama', 'version': '1.0', 'running': False, 'metadata': {}}

    def report(self, assets, observed=None):
        return {'scan_id': utc_now(), 'observed_at': observed or utc_now(),
                'device': {'hostname': 'PRIVATE_HOST'}, 'assets': assets, 'privacy': {}}

    def events(self, name):
        with self.db.connect() as conn:
            events = [json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM otlp_outbox')]
        return [e for e in events if e['event.name'] == name]

    def test_stopped_removed_reappeared_and_version_visible(self):
        for assets, present in (([self.asset], True), ([], False), ([self.asset], True)):
            self.db.ingest(self.device, self.report(assets))
            row = self.db.summary()['items'][0]
            self.assertEqual(row['present'], present)
            self.assertFalse(row['running'])
            self.assertEqual(row['version'], '1.0')
            event = self.events(EVENT_NAME)[0]
            self.assertIs(event['attributes']['asset.present'], present)
            self.assertFalse(event['attributes']['asset.running'])
            self.assertEqual(self.db.list_assets()[0]['present'], present)
            exported = next(csv.DictReader(io.StringIO(self.db.export_csv())))
            self.assertEqual(exported['status'], 'Observed' if present else 'Absent')

    def test_heartbeat_only_on_new_current_snapshot_and_not_enrollment(self):
        self.assertEqual(self.events(DEVICE_EVENT_NAME), [])
        first = self.report([self.asset])
        self.db.ingest(self.device, first)
        initial = self.events(DEVICE_EVENT_NAME)[0]
        asset_id = self.events(EVENT_NAME)[0]['attributes']['edgedisco.observation.id']
        self.db.ingest(self.device, first)  # Idempotent retry.
        self.assertEqual(self.events(DEVICE_EVENT_NAME), [initial])
        self.db.ingest(self.device, self.report([], '2000-01-01T00:00:00+00:00'))
        self.assertEqual(self.events(DEVICE_EVENT_NAME), [initial])
        self.db.ingest(self.device, self.report([self.asset]))
        latest = self.events(DEVICE_EVENT_NAME)[0]
        self.assertNotEqual(initial, latest)
        self.assertEqual(self.events(EVENT_NAME)[0]['attributes']['edgedisco.observation.id'], asset_id)
        self.assertEqual(latest['attributes']['inventory.asset_count'], 1)
        self.assertEqual(latest['attributes']['inventory.simulated_asset_count'], 0)
        for value in ('PRIVATE_HOST', self.token, 'fingerprint'):
            self.assertNotIn(value, json.dumps(latest))
        self.db.ingest(self.device, self.report([]))
        self.assertEqual(self.events(DEVICE_EVENT_NAME)[0]['attributes']['inventory.asset_count'], 0)

    def test_asset_state_wins_over_heartbeat_under_capacity_pressure(self):
        constrained = Database(self.path, otlp_enabled=True, otlp_max_pending=1)
        constrained.ingest(self.device, self.report([self.asset]))
        self.assertEqual(len(self.events(EVENT_NAME)), 1)
        self.assertEqual(self.events(DEVICE_EVENT_NAME), [])

    def test_old_fingerprint_cannot_override_current_version(self):
        self.db.ingest(self.device, self.report([self.asset]))
        replacement = {**self.asset, 'fingerprint': 'b' * 64, 'version': '2.0'}
        self.db.ingest(self.device, self.report([replacement]))
        event = self.events(EVENT_NAME)[0]
        self.assertEqual(event['attributes']['asset.version'], '2.0')
        self.assertTrue(event['attributes']['asset.present'])
        # Reopening must not emit an obsolete version into the MCP feed.
        Database(self.path)
        from ai_asset_inventory.inventory_sync import snapshot
        with self.db.connect() as conn:
            self.assertEqual(snapshot(conn)['items'][0]['attributes']['asset.version'], '2.0')

        self.db.ingest(self.device, self.report([]))
        self.assertEqual(self.events(EVENT_NAME)[0]['attributes']['asset.version'], '2.0')
        self.assertFalse(self.events(EVENT_NAME)[0]['attributes']['asset.present'])

    def test_delayed_scan_is_stale_without_fabricating_stop(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        self.db.ingest(self.device, self.report([{**self.asset, 'running': True}], old))
        item = self.db.summary()['items'][0]
        self.assertTrue(item['stale'])
        self.assertFalse(item['running'])
        self.assertTrue(item['present'])  # Last observed presence, qualified by stale.
        self.assertFalse(self.db.inventory_device_status()['items'][0]['fresh'])
        self.assertTrue(self.events(EVENT_NAME)[0]['attributes']['asset.running'])
        self.assertEqual(self.events(DEVICE_EVENT_NAME)[0]['timestamp'], old)

    def test_migration_does_not_guess_presence_or_emit_telemetry(self):
        self.db.ingest(self.device, self.report([self.asset]))
        with self.db.connect() as conn:
            conn.execute('ALTER TABLE assets DROP COLUMN present')
            conn.execute('PRAGMA user_version=3')
            before = conn.execute('SELECT COUNT(*) FROM otlp_outbox').fetchone()[0]
        OutboxStore(self.path)  # Worker must not mark asset migration complete.
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
        self.db = Database(self.path, otlp_enabled=True)
        self.assertIsNone(self.db.summary()['items'][0]['present'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM otlp_outbox').fetchone()[0], before)
        self.db.ingest(self.device, self.report([]))
        self.assertFalse(self.db.summary()['items'][0]['present'])

    def test_endpoint_cannot_forge_presence(self):
        forged = self.report([{**self.asset, 'present': False}])
        with self.assertRaises(ValueError):
            validate_report(forged)

    def test_encoder_separates_event_allowlists_and_rejects_bad_types(self):
        self.db.ingest(self.device, self.report([self.asset]))
        heartbeat = self.events(DEVICE_EVENT_NAME)[0]
        asset = self.events(EVENT_NAME)[0]
        for event, field, value in (
            (heartbeat, 'hostname', 'SECRET'), (heartbeat, 'asset.name', 'Ollama'),
            (heartbeat, 'inventory.asset_count', True), (heartbeat, 'inventory.asset_count', -1),
            (heartbeat, 'inventory.asset_count', 10001),
            (heartbeat, 'inventory.simulated_asset_count', 2),
            (asset, 'inventory.asset_count', 1), (asset, 'asset.present', 'false'),
        ):
            bad = copy.deepcopy(event)
            bad['attributes'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _validated_event(json.dumps(bad))
        asset['attributes']['asset.running'] = True
        asset['attributes']['asset.present'] = False
        with self.assertRaises(ValueError):
            _validated_event(json.dumps(asset))
        asset['attributes']['edgedisco.schema.version'] = 1
        del asset['attributes']['asset.present']
        _validated_event(json.dumps(asset))  # Older queued events remain valid.

    def test_both_event_types_round_trip_to_protobuf(self):
        try:
            from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
        except ImportError:
            self.skipTest('OTLP extra required')
        self.db.ingest(self.device, self.report([self.asset]))
        for name in (EVENT_NAME, DEVICE_EVENT_NAME):
            event = self.events(name)[0]
            wire = encode_outbox_event(json.dumps(event))
            record = ExportLogsServiceRequest.FromString(wire).resource_logs[0].scope_logs[0].log_records[0]
            self.assertEqual(record.event_name, name)
            if name == DEVICE_EVENT_NAME:
                self.assertEqual(record.body.string_value, name)
            self.assertLessEqual(record.time_unix_nano, record.observed_time_unix_nano)
            self.assertNotIn(b'PRIVATE_HOST', wire)
