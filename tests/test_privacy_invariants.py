import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.database import Database
from ai_asset_inventory.otlp_encoder import encode_outbox_event
from ai_asset_inventory.otlp_events import project_asset

try:
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
except ImportError:
    ExportLogsServiceRequest = None

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_otlp"


class PrivacyInvariantsTests(unittest.TestCase):
    def setUp(self):
        canary_file = FIXTURES_DIR / "privacy_canaries.json"
        self.assertTrue(canary_file.exists(), "Missing privacy canaries fixture")
        self.canaries = json.loads(canary_file.read_text(encoding="utf-8"))
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "inventory.db", otlp_enabled=True)
        self.device_id, self.device_token = self.db.enroll({"hostname": "host", "os": "Darwin"})

    def tearDown(self):
        self.temp.cleanup()

    def _all_canary_strings(self):
        for category, items in self.canaries.items():
            for item in items:
                yield category, item

    def test_injected_metadata_canaries_never_projected(self):
        """Metadata keys/values containing sensitive data are completely dropped during projection."""
        observed_at = "2026-09-21T12:00:00+00:00"
        for category, canary in self._all_canary_strings():
            asset = {
                "kind": "application",
                "name": "Ollama",
                "vendor": "Ollama",
                "running": True,
                "present": True,
                "metadata": {
                    "canary_field": canary,
                    "prompt": canary,
                    "command_line": canary,
                    "secret": canary,
                }
            }
            res = project_asset(self.device_id, observed_at, asset)
            self.assertIsNotNone(res, f"Valid asset should project even with noisy metadata (category: {category})")
            if res is not None:
                logical_key, state_hash, event = res
                serialized = json.dumps(event)
                self.assertNotIn(canary, serialized, f"Privacy leak: canary found in projected event! Category: {category}")

    def test_canary_in_asset_identity_fails_closed(self):
        """Canary strings placed in core identity fields reject projection entirely."""
        observed_at = "2026-09-21T12:00:00+00:00"
        for category, canary in self._all_canary_strings():
            # Injected name
            bad_name = {
                "kind": "application",
                "name": f"Ollama {canary}",
                "vendor": "Ollama",
                "running": True,
                "present": True,
            }
            self.assertIsNone(project_asset(self.device_id, observed_at, bad_name))

            # Injected vendor
            bad_vendor = {
                "kind": "application",
                "name": "Ollama",
                "vendor": canary,
                "running": True,
                "present": True,
            }
            self.assertIsNone(project_asset(self.device_id, observed_at, bad_vendor))

            # Injected version (>128 chars)
            if len(canary) > 128:
                bad_version = {
                    "kind": "application",
                    "name": "Ollama",
                    "vendor": "Ollama",
                    "version": canary,
                    "running": True,
                    "present": True,
                }
                self.assertIsNone(project_asset(self.device_id, observed_at, bad_version))

    def test_outbox_sqlite_and_protobuf_contain_zero_canaries(self):
        """End-to-end report ingest into DB: outbox payload and protobuf encoding contain 0 canaries."""
        for category, canary in self._all_canary_strings():
            report_data = {
                "scan_id": f"scan-{category}",
                "observed_at": "2026-09-21T12:00:00+00:00",
                "privacy": {"content_captured": False},
                "assets": [{
                    "fingerprint": f"fp-{category}",
                    "name": "CrewAI",
                    "vendor": "CrewAI",
                    "kind": "agent_runtime",
                    "running": True,
                    "present": True,
                    "metadata": {
                        "host_app": "Cursor",
                        "relationship": "spawned_by",
                        "prompt": canary,
                        "token": canary,
                        "raw_cmd": canary,
                    }
                }]
            }
            self.db.ingest(self.device_id, report_data)

            # Query raw outbox payloads
            with self.db.connect() as conn:
                rows = conn.execute("SELECT payload_json FROM otlp_outbox WHERE status='pending'").fetchall()
                for row in rows:
                    payload = row["payload_json"]
                    self.assertNotIn(canary, payload, f"Privacy leak in SQLite outbox payload! Category: {category}")
                    # Test encoded protobuf bytes if optional OTLP dependencies are installed
                    if ExportLogsServiceRequest is not None:
                        encoded = encode_outbox_event(payload)
                        canary_bytes = canary.encode("utf-8")
                        self.assertNotIn(canary_bytes, encoded, f"Privacy leak in Protobuf payload! Category: {category}")


if __name__ == "__main__":
    unittest.main()
