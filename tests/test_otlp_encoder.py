import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.database import Database
from ai_asset_inventory.otlp_encoder import (
    OTLP_CONTENT_TYPE, OTLP_LOGS_PATH, encode_outbox_event,
)

try:
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
except ImportError:
    ExportLogsServiceRequest = None


SENTINELS = (
    "EDGEDISCO_SECRET_SENTINEL", "EDGEDISCO_PROMPT_SENTINEL",
    "EDGEDISCO_RESPONSE_SENTINEL", "EDGEDISCO_COMMAND_SENTINEL",
    "EDGEDISCO_SOURCE_SENTINEL", "EDGEDISCO_PATH_SENTINEL",
    "EDGEDISCO_METADATA_SENTINEL",
)


@unittest.skipUnless(ExportLogsServiceRequest, "optional OTLP protobuf dependency is not installed")
class OtlpEncoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "inventory.db", otlp_enabled=True)
        self.device_id, self.device_token = self.db.enroll({"hostname": "host", "os": "Darwin"})

    def tearDown(self):
        self.temp.cleanup()

    def persist(self, *, simulated=False):
        metadata = {
            "host_app": "Cursor", "relationship": "spawned_by",
            "prompt": SENTINELS[1], "response": SENTINELS[2],
            "api_token": SENTINELS[0], "raw_command_line": SENTINELS[3],
            "source_code": SENTINELS[4], "filesystem_path": SENTINELS[5],
            "nested": {"unsafe": SENTINELS[6]},
        }
        if simulated:
            metadata.update(demo_lab=True, evidence_label="SIMULATED TEST WORKLOADS")
        report = {
            "scan_id": "scan-demo" if simulated else "scan-real",
            "observed_at": "2026-09-20T18:00:00+00:00",
            "privacy": {"content_captured": False, "secret": SENTINELS[0]},
            "device": {"raw_path": SENTINELS[5]},
            "assets": [{
                "fingerprint": "runtime-demo" if simulated else "runtime-real",
                "kind": "agent_runtime", "name": "CrewAI", "vendor": "CrewAI",
                "running": not simulated, "metadata": metadata,
                "command_hash": SENTINELS[3], "path_hash": SENTINELS[5],
                "credentials": SENTINELS[0], "source_code": SENTINELS[4],
            }],
        }
        self.assertEqual(self.db.ingest(self.device_id, report), 1)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id,payload_json FROM otlp_outbox ORDER BY created_at DESC,id DESC"
            ).fetchall()
        return next(
            row for row in rows
            if json.loads(row["payload_json"])["attributes"]["edgedisco.simulated"] is simulated
        )

    @staticmethod
    def decoded(payload):
        wire = encode_outbox_event(payload)
        decoded = ExportLogsServiceRequest()
        decoded.ParseFromString(wire)
        resource_logs = decoded.resource_logs[0]
        record = resource_logs.scope_logs[0].log_records[0]
        attrs = {}
        for item in record.attributes:
            field = item.value.WhichOneof("value")
            attrs[item.key] = getattr(item.value, field)
        resources = {item.key: item.value.string_value for item in resource_logs.resource.attributes}
        return wire, decoded, resources, attrs, record

    def test_round_trip_from_persisted_sanitized_event(self):
        row = self.persist()
        wire, decoded, resources, attrs, record = self.decoded(row["payload_json"])
        self.assertTrue(wire)
        self.assertEqual(len(decoded.resource_logs), 1)
        self.assertEqual(len(decoded.resource_logs[0].scope_logs), 1)
        self.assertEqual(resources, {"service.name": "edgedisco", "service.version": "0.5.0"})
        self.assertEqual(record.event_name, "edgedisco.asset.observed")
        self.assertEqual(record.severity_number, 9)
        self.assertEqual(record.time_unix_nano, 1789927200000000000)
        self.assertGreater(record.observed_time_unix_nano, record.time_unix_nano)
        self.assertEqual(attrs, {
            "edgedisco.schema.version": 1,
            "edgedisco.observation.id": row["id"],
            "device.id": self.device_id,
            "asset.kind": "agent_runtime", "asset.name": "CrewAI",
            "asset.vendor": "CrewAI", "asset.running": True,
            "asset.host_app": "Cursor", "asset.relationship": "spawned_by",
            "edgedisco.simulated": False,
        })
        self.assertEqual(OTLP_LOGS_PATH, "/v1/logs")
        self.assertEqual(OTLP_CONTENT_TYPE, "application/x-protobuf")

    def test_adversarial_input_cannot_enter_protobuf(self):
        for simulated in (False, True):
            row = self.persist(simulated=simulated)
            wire, decoded, _, attrs, _ = self.decoded(row["payload_json"])
            self.assertEqual(attrs["edgedisco.simulated"], simulated)
            self.assertEqual(attrs["asset.running"], not simulated)
            rendered = str(decoded)
            for sentinel in SENTINELS + (self.device_token,):
                self.assertNotIn(sentinel, row["payload_json"])
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn(sentinel.encode(), wire)

    def test_encoder_rejects_extra_fields_even_in_outbox_json(self):
        event = json.loads(self.persist()["payload_json"])
        event["attributes"]["prompt"] = SENTINELS[1]
        with self.assertRaisesRegex(ValueError, "unsupported outbox attributes"):
            encode_outbox_event(json.dumps(event))
        event["attributes"].pop("prompt")
        event["metadata"] = {"source_code": SENTINELS[4]}
        with self.assertRaisesRegex(ValueError, "unsupported outbox event fields"):
            encode_outbox_event(json.dumps(event))


if __name__ == "__main__":
    unittest.main()
