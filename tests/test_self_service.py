import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.self_service import (
    AGENT_LABEL,
    SERVER_LABEL,
    default_layout,
    ensure_credentials,
    setup_macos,
    uninstall_macos,
    write_launch_agents,
)


class FakeAgentClient:
    def __init__(self, path):
        self.path = Path(path)

    def enroll(self):
        data = json.loads(self.path.read_text())
        data.pop("enrollment_token", None)
        data.update({"device_id": "device", "device_token": "token"})
        self.path.write_text(json.dumps(data))

    def send_once(self):
        return {"asset_count": 4, "runtime_event_count": 1}


class SelfServiceTests(unittest.TestCase):
    def test_credentials_are_persistent_and_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = default_layout(Path(temp) / ".edgedisco", Path(temp))
            first = ensure_credentials(layout, 9090)
            second = ensure_credentials(layout)
            self.assertEqual(first["AAI_ADMIN_TOKEN"], second["AAI_ADMIN_TOKEN"])
            self.assertNotEqual(first["AAI_ADMIN_TOKEN"], first["AAI_ENROLLMENT_TOKEN"])
            self.assertEqual(second["EDGEDISCO_PORT"], "9090")

    def test_launch_agents_do_not_contain_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = default_layout(Path(temp) / ".edgedisco", Path(temp))
            ensure_credentials(layout)
            server, agent = write_launch_agents(layout, 8080)
            server_data = plistlib.loads(server.read_bytes())
            agent_data = plistlib.loads(agent.read_bytes())
            self.assertEqual(server_data["Label"], SERVER_LABEL)
            self.assertEqual(agent_data["Label"], AGENT_LABEL)
            self.assertNotIn("AAI_ADMIN_TOKEN", server.read_text())

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._health", return_value=None)
    @patch("ai_asset_inventory.self_service._restart_service")
    @patch("ai_asset_inventory.self_service._wait_for_server")
    @patch("ai_asset_inventory.self_service.install_adapters", return_value=[])
    @patch("ai_asset_inventory.self_service.detect_adapters", return_value=["cursor"])
    @patch("ai_asset_inventory.self_service.AgentClient", FakeAgentClient)
    def test_setup_enrolls_and_sends_initial_inventory(
        self, _detect, _install, _wait, restart, _health, _system
    ):
        with tempfile.TemporaryDirectory() as temp:
            result = setup_macos(
                root=Path(temp) / ".edgedisco", home=Path(temp), open_dashboard=False
            )
            self.assertEqual(result["asset_count"], 4)
            self.assertEqual(result["adapters"], ["cursor"])
            self.assertEqual(restart.call_count, 2)

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_uninstall_preserves_data_by_default(self, launchctl, _system):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / ".edgedisco"
            root.mkdir()
            (root / "evidence").write_text("keep")
            result = uninstall_macos(root=root, home=home)
            self.assertTrue(root.exists())
            self.assertFalse(result["data_purged"])
            self.assertEqual(launchctl.call_count, 2)


if __name__ == "__main__":
    unittest.main()
