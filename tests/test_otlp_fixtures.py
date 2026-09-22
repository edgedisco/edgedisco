import json
import unittest
from pathlib import Path

from ai_asset_inventory.otlp_encoder import encode_outbox_event

try:
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
except ImportError:
    ExportLogsServiceRequest = None

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_otlp"


@unittest.skipUnless(ExportLogsServiceRequest, "optional OTLP protobuf dependency is not installed")
class OtlpFixturesTests(unittest.TestCase):
    def test_golden_observation_v2_matches_wire_format(self):
        json_file = FIXTURES_DIR / "observation_v2.json"
        pb_file = FIXTURES_DIR / "observation_v2.pb"
        self.assertTrue(json_file.exists(), f"Missing fixture: {json_file}")
        self.assertTrue(pb_file.exists(), f"Missing fixture: {pb_file}")

        payload_json = json_file.read_text(encoding="utf-8")
        expected_pb = pb_file.read_bytes()

        # Re-encode and verify byte-for-byte deterministic match
        encoded = encode_outbox_event(payload_json)
        self.assertEqual(encoded, expected_pb)

        # Decode protobuf and assert structure
        if ExportLogsServiceRequest is None:
            self.skipTest("optional OTLP protobuf dependency is not installed")
        req = ExportLogsServiceRequest()
        req.ParseFromString(encoded)
        self.assertEqual(len(req.resource_logs), 1)
        r_log = req.resource_logs[0]
        
        # Resource attributes
        res_attrs = {kv.key: kv.value.string_value for kv in r_log.resource.attributes}
        self.assertEqual(res_attrs["service.name"], "edgedisco")

        # Scope logs
        self.assertEqual(len(r_log.scope_logs), 1)
        s_log = r_log.scope_logs[0]
        self.assertEqual(s_log.scope.name, "ai_asset_inventory.otlp_encoder")
        self.assertEqual(s_log.scope.version, "0.5.0")

        # Log record
        self.assertEqual(len(s_log.log_records), 1)
        rec = s_log.log_records[0]
        self.assertEqual(rec.event_name, "edgedisco.asset.observed")
        self.assertEqual(rec.severity_number, 9)
        self.assertEqual(rec.body.string_value, "")

        # Record attributes
        attr_map = {}
        for kv in rec.attributes:
            if kv.value.HasField("string_value"):
                attr_map[kv.key] = kv.value.string_value
            elif kv.value.HasField("int_value"):
                attr_map[kv.key] = kv.value.int_value
            elif kv.value.HasField("bool_value"):
                attr_map[kv.key] = kv.value.bool_value

        self.assertEqual(attr_map["edgedisco.schema.version"], 2)
        self.assertEqual(attr_map["device.id"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(attr_map["asset.name"], "Claude Code")
        self.assertEqual(attr_map["asset.vendor"], "Anthropic")
        self.assertEqual(attr_map["asset.kind"], "agent_runtime")
        self.assertEqual(attr_map["asset.running"], True)
        self.assertEqual(attr_map["asset.present"], True)
        self.assertEqual(attr_map["asset.host_app"], "Direct/local")
        self.assertEqual(attr_map["asset.relationship"], "local_process")
        self.assertEqual(attr_map["edgedisco.simulated"], False)

    def test_golden_device_inventory_v2_matches_wire_format(self):
        json_file = FIXTURES_DIR / "device_inventory_v2.json"
        pb_file = FIXTURES_DIR / "device_inventory_v2.pb"
        self.assertTrue(json_file.exists(), f"Missing fixture: {json_file}")
        self.assertTrue(pb_file.exists(), f"Missing fixture: {pb_file}")

        payload_json = json_file.read_text(encoding="utf-8")
        expected_pb = pb_file.read_bytes()

        # Re-encode and verify byte-for-byte deterministic match
        encoded = encode_outbox_event(payload_json)
        self.assertEqual(encoded, expected_pb)

        if ExportLogsServiceRequest is None:
            self.skipTest("optional OTLP protobuf dependency is not installed")
        req = ExportLogsServiceRequest()
        req.ParseFromString(encoded)
        rec = req.resource_logs[0].scope_logs[0].log_records[0]
        self.assertEqual(rec.event_name, "edgedisco.device.inventory")
        self.assertEqual(rec.body.string_value, "edgedisco.device.inventory")

        attr_map = {kv.key: kv.value.int_value if kv.value.HasField("int_value") else kv.value.string_value
                    for kv in rec.attributes}
        self.assertEqual(attr_map["inventory.asset_count"], 5)
        self.assertEqual(attr_map["inventory.simulated_asset_count"], 1)


if __name__ == "__main__":
    unittest.main()
