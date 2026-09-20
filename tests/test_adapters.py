import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_inventory.adapters import install_claude, install_copilot, install_cursor


class AdapterTests(unittest.TestCase):
    def test_cursor_installer_preserves_existing_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "hooks.json"
            config = Path(temp) / "agent.json"
            target.write_text(json.dumps({"version": 1, "hooks": {"sessionStart": [{"command": "existing"}]}}))
            install_cursor(config, target)
            data = json.loads(target.read_text())
            commands = [item["command"] for item in data["hooks"]["sessionStart"]]
            self.assertIn("existing", commands)
            self.assertTrue(any("ai_asset_inventory" in command for command in commands))
            self.assertTrue(target.with_suffix(".json.edgedisco.bak").exists())

    def test_claude_and_copilot_configs_are_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "agent.json"
            claude = install_claude(config, root / "claude.json")
            copilot = install_copilot(config, root / "copilot.json")
            self.assertIn("SessionStart", json.loads(claude.read_text())["hooks"])
            self.assertIn("sessionStart", json.loads(copilot.read_text())["hooks"])


if __name__ == "__main__":
    unittest.main()
