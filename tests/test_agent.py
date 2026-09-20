from __future__ import annotations

import json
import queue
import threading
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
            pending = Mock()
            pending.get.side_effect = [client.scan_payload(items) for items in (first, first, changed)] + [KeyboardInterrupt()]
            stop = Mock()
            stop.is_set.return_value = False
            client._request = Mock(return_value={"asset_count": 1})
            client.flush_runtime_events = Mock(return_value=0)
            with patch("ai_asset_inventory.agent.time.monotonic", return_value=10.0):
                with self.assertRaises(KeyboardInterrupt):
                    client._upload_inventory(pending, stop, 300)
        report_calls = [call for call in client._request.call_args_list if call.args[0] == "/api/v1/reports"]
        self.assertEqual(len(report_calls), 2)
        client.flush_runtime_events.assert_not_called()

    def test_blocked_upload_does_not_block_collection_and_queue_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "agent.json"
            config.write_text('{"device_token":"test"}')
            client = AgentClient(config)
            pending = queue.Queue(maxsize=1)
            stop = threading.Event()
            entered = threading.Event()
            release = threading.Event()
            def upload(*args):
                entered.set()
                release.wait(2)
                return {}
            client._request = Mock(side_effect=upload)
            client.scanner = Mock()
            count = 0
            def collect():
                nonlocal count
                count += 1
                if count == 1:
                    pending.put(client.scan_payload([_asset('0' * 64)]))
                    self.assertTrue(entered.wait(2))
                if count == 4:
                    stop.set()
                return [_asset(f'{count:064x}')]
            client.scanner.collect_inventory.side_effect = collect
            worker = threading.Thread(target=client._upload_inventory, args=(pending, stop, 300))
            worker.start()
            try:
                client._collect_loop(pending, stop, .001)
                self.assertEqual(count, 4)
                self.assertEqual(pending.qsize(), 1)
                self.assertEqual(pending.get_nowait()['assets'][0]['fingerprint'], f'{4:064x}')
                self.assertEqual(client._request.call_count, 1)
            finally:
                stop.set()
                release.set()
                worker.join(2)

    def test_failed_upload_is_not_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.json'
            config.write_text('{"device_token":"test"}')
            client = AgentClient(config)
            payload = client.scan_payload([_asset('a' * 64)])
            pending = Mock()
            pending.get.side_effect = [payload, payload, payload, KeyboardInterrupt()]
            stop = Mock()
            stop.is_set.return_value = False
            client._request = Mock(side_effect=[OSError('offline'), {}])
            with self.assertRaises(KeyboardInterrupt):
                client._upload_inventory(pending, stop, 300)
            self.assertEqual(client._request.call_count, 2)
            stop.wait.assert_called_once_with(5)

    def test_unchanged_state_is_uploaded_at_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.json'
            config.write_text('{"device_token":"test"}')
            client = AgentClient(config)
            payload = client.scan_payload([_asset('a' * 64)])
            pending = Mock()
            pending.get.side_effect = [payload, payload, KeyboardInterrupt()]
            stop = Mock()
            stop.is_set.return_value = False
            client._request = Mock(return_value={})
            with patch('ai_asset_inventory.agent.time.monotonic', side_effect=[0, 301, 301]):
                with self.assertRaises(KeyboardInterrupt):
                    client._upload_inventory(pending, stop, 300)
            self.assertEqual(client._request.call_count, 2)

    def test_stale_queued_snapshot_is_not_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.json'
            config.write_text('{"device_token":"test"}')
            client = AgentClient(config)
            payload = client.scan_payload([])
            payload['observed_at'] = '2000-01-01T00:00:00+00:00'
            pending = Mock()
            pending.get.side_effect = [payload, KeyboardInterrupt()]
            stop = Mock()
            stop.is_set.return_value = False
            client._request = Mock()
            with self.assertRaises(KeyboardInterrupt):
                client._upload_inventory(pending, stop, 300)
            client._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
