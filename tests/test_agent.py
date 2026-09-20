from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_asset_inventory.agent import AgentClient, _inventory_state
from ai_asset_inventory.models import Asset


def _asset(fingerprint: str, *, running: bool = True) -> Asset:
    return Asset(
        fingerprint=fingerprint,
        kind="process",
        name="OpenAI Codex",
        vendor="OpenAI",
        running=running,
    )


class IncrementalAgentTests(unittest.TestCase):
    def test_inventory_state_is_order_independent_and_tracks_changes(self):
        first = _asset("a" * 64)
        second = _asset("b" * 64)
        self.assertEqual(_inventory_state([first, second]), _inventory_state([second, first]))
        self.assertNotEqual(_inventory_state([first]), _inventory_state([_asset("a" * 64, running=False)]))

    def test_run_uploads_on_change_and_uses_heartbeat_for_unchanged_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agent.json"
            config.write_text(json.dumps({
                "server_url": "http://127.0.0.1:8080",
                "device_token": "device-token",
                "scan_interval_seconds": 300,
                "process_poll_interval_seconds": 10,
                "static_scan_interval_seconds": 900,
            }))
            client = AgentClient(config)
            first = [_asset("a" * 64)]
            changed = [_asset("a" * 64), _asset("b" * 64)]
            client.scanner = Mock()
            client.scanner.collect_inventory.side_effect = [first, first, changed, KeyboardInterrupt()]
            client._request = Mock(return_value={"asset_count": 1})
            client.flush_runtime_events = Mock(return_value=0)
            with patch("ai_asset_inventory.agent.time.monotonic", side_effect=[0.0, 10.0, 20.0]), \
                 patch("ai_asset_inventory.agent.time.sleep"):
                with self.assertRaises(KeyboardInterrupt):
                    client.run()
        report_calls = [call for call in client._request.call_args_list if call.args[0] == "/api/v1/reports"]
        self.assertEqual(len(report_calls), 2)
        self.assertEqual(client.flush_runtime_events.call_count, 3)


if __name__ == "__main__":
    unittest.main()
