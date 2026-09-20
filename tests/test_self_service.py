import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.self_service import (
    AGENT_LABEL,
    LAUNCHER_MARKER,
    PATH_MARKER_BEGIN,
    PATH_MARKER_END,
    SERVER_LABEL,
    default_layout,
    ensure_credentials,
    install_cli_launcher,
    ensure_local_server,
    setup_macos,
    uninstall_cli_launcher,
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
    @patch("ai_asset_inventory.self_service._health", return_value={"assets": 0})
    @patch("ai_asset_inventory.self_service.setup_macos")
    def test_demo_reuses_healthy_server(self, setup, health):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = default_layout(home / ".edgedisco", home)
            layout.env.parent.mkdir(parents=True)
            layout.env.write_text("AAI_ADMIN_TOKEN=test-token\nEDGEDISCO_PORT=8765\n")
            server = ensure_local_server(root=layout.root, home=home)
            self.assertTrue(server.reused)
            self.assertEqual(server.port, 8765)
            setup.assert_not_called()
            health.assert_called_once_with(8765, "test-token")

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._health", side_effect=[None, {"assets": 0}])
    @patch("ai_asset_inventory.self_service.setup_macos")
    def test_demo_starts_server_once_when_missing(self, setup, health, system):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = default_layout(home / ".edgedisco", home)
            layout.env.parent.mkdir(parents=True)
            layout.env.write_text("AAI_ADMIN_TOKEN=test-token\nEDGEDISCO_PORT=8765\n")
            server = ensure_local_server(root=layout.root, home=home)
            self.assertFalse(server.reused)
            setup.assert_called_once_with(root=layout.root, home=home, open_dashboard=False)

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
            self.assertTrue(Path(result["cli"]).is_symlink())

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


class CliLauncherTests(unittest.TestCase):
    def _layout_with_fake_cli(self, home: Path):
        layout = default_layout(home / ".edgedisco", home)
        layout.venv.mkdir(parents=True)
        cli = layout.venv / "bin" / "edgedisco"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\necho fake-edgedisco \"$@\"\n")
        cli.chmod(0o755)
        return layout, cli

    def test_install_creates_launcher_pointing_at_managed_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout, cli = self._layout_with_fake_cli(home)
            (home / "pre-existing.txt").write_text("keep-me")
            unrelated = home / ".local" / "bin" / "other-tool"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("#!/bin/sh\n")
            profile = home / ".zprofile"
            profile.write_text("export PATH=\"/opt/custom/bin:$PATH\"\n")

            launcher = install_cli_launcher(layout, home)

            self.assertEqual(launcher.public, home / ".local" / "bin" / "edgedisco")
            self.assertTrue(launcher.public.is_symlink())
            self.assertEqual(launcher.public.resolve(), launcher.managed.resolve())
            self.assertIn(LAUNCHER_MARKER, launcher.managed.read_text())
            self.assertIn(str(cli), launcher.managed.read_text())
            self.assertTrue(launcher.path_integrated)
            self.assertTrue(unrelated.exists())
            self.assertEqual((home / "pre-existing.txt").read_text(), "keep-me")
            text = profile.read_text()
            self.assertIn("/opt/custom/bin", text)
            self.assertIn(PATH_MARKER_BEGIN, text)
            self.assertIn(str(home / ".local" / "bin"), text)

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout, _cli = self._layout_with_fake_cli(home)
            first = install_cli_launcher(layout, home)
            second = install_cli_launcher(layout, home)
            self.assertEqual(first.public, second.public)
            self.assertTrue(second.public.is_symlink())
            profile = (home / ".zprofile").read_text()
            self.assertEqual(profile.count(PATH_MARKER_BEGIN), 1)
            self.assertEqual(profile.count(PATH_MARKER_END), 1)

    def test_uninstall_removes_launcher_and_path_block_only(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout, _cli = self._layout_with_fake_cli(home)
            unrelated = home / ".local" / "bin" / "other-tool"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("#!/bin/sh\n")
            (home / ".zprofile").write_text(
                "export PATH=\"/opt/custom/bin:$PATH\"\n"
                "export IMPORTANT=1\n"
            )
            install_cli_launcher(layout, home)

            result = uninstall_cli_launcher(layout, home)

            self.assertFalse((home / ".local" / "bin" / "edgedisco").exists())
            self.assertFalse(layout.bin.joinpath("edgedisco").exists())
            self.assertFalse((layout.root / "cli-launcher.path").exists())
            self.assertTrue(unrelated.exists())
            profile = (home / ".zprofile").read_text()
            self.assertIn("/opt/custom/bin", profile)
            self.assertIn("IMPORTANT=1", profile)
            self.assertNotIn(PATH_MARKER_BEGIN, profile)
            self.assertNotIn(PATH_MARKER_END, profile)
            self.assertTrue(result["path_block_removed"])

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_uninstall_macos_removes_launcher_without_purging_data(self, _launchctl, _system):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout, _cli = self._layout_with_fake_cli(home)
            (layout.root / "evidence").write_text("keep")
            install_cli_launcher(layout, home)

            result = uninstall_macos(root=layout.root, home=home)

            self.assertTrue(layout.root.exists())
            self.assertEqual((layout.root / "evidence").read_text(), "keep")
            self.assertFalse((home / ".local" / "bin" / "edgedisco").exists())
            self.assertIn(str(home / ".local" / "bin" / "edgedisco"), result["launcher_removed"])

    def test_preserves_unrelated_edgedisco_command_and_uses_managed_bin(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout, _cli = self._layout_with_fake_cli(home)
            public = home / ".local" / "bin" / "edgedisco"
            public.parent.mkdir(parents=True)
            public.write_text("#!/bin/sh\necho not-ours\n")
            launcher = install_cli_launcher(layout, home)
            self.assertEqual(public.read_text(), "#!/bin/sh\necho not-ours\n")
            self.assertEqual(launcher.public, layout.bin / "edgedisco")
            self.assertIn(str(layout.bin), (home / ".zprofile").read_text())
            uninstall_cli_launcher(layout, home)
            self.assertEqual(public.read_text(), "#!/bin/sh\necho not-ours\n")


if __name__ == "__main__":
    unittest.main()
