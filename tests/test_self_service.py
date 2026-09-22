import json
import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory.self_service import (
    AGENT_LABEL,
    EXPORTER_LABEL,
    LAUNCHER_MARKER,
    PATH_MARKER_BEGIN,
    PATH_MARKER_END,
    SERVER_LABEL,
    Layout,
    default_layout,
    ensure_credentials,
    install_cli_launcher,
    open_dashboard,
    ensure_local_server,
    restart_linux,
    restart_macos,
    restart_services,
    setup_linux,
    setup_macos,
    start_linux,
    start_macos,
    start_services,
    stop_linux,
    stop_macos,
    stop_services,
    _restart_service,
    _verify_browser_bootstrap,
    _health,
    uninstall_cli_launcher,
    uninstall_macos,
    uninstall_linux,
    write_systemd_units,
    write_launch_agents,
)


class CliPrivacyTests(unittest.TestCase):
    @patch("ai_asset_inventory.cli.setup_self_service")
    def test_setup_does_not_print_admin_token(self, setup):
        from ai_asset_inventory.cli import main
        setup.return_value = {
            "dashboard": "http://127.0.0.1:8080", "admin_token": "private-test-token",
            "adapters": [], "asset_count": 0, "cli": "/tmp/edgedisco",
        }
        out = io.StringIO()
        with patch("sys.argv", ["edgedisco", "setup", "--no-open"]), redirect_stdout(out):
            main()
        self.assertNotIn("private-test-token", out.getvalue())


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
    @patch("ai_asset_inventory.self_service.platform.system", return_value="Linux")
    @patch("ai_asset_inventory.self_service.require_systemd_user")
    @patch("ai_asset_inventory.self_service._health", return_value=None)
    @patch("ai_asset_inventory.self_service._restart_systemd_service")
    @patch("ai_asset_inventory.self_service._wait_for_server")
    @patch("ai_asset_inventory.self_service._verify_browser_bootstrap")
    @patch("ai_asset_inventory.self_service.install_adapters", return_value=[])
    @patch("ai_asset_inventory.self_service.detect_adapters", return_value=[])
    @patch("ai_asset_inventory.self_service.AgentClient", FakeAgentClient)
    def test_linux_setup_writes_and_starts_user_services(
        self, _detect, _install, verify, _wait, restart, _health, required, _system
    ):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / ".edgedisco"
            result = setup_linux(root=root, home=home, open_dashboard=False)
            required.assert_called_once_with()
            self.assertTrue((home / ".config/systemd/user/com.edgedisco.server.service").exists())
            self.assertTrue((home / ".config/systemd/user/com.edgedisco.agent.service").exists())
            self.assertEqual(restart.call_args_list[0].args, ("com.edgedisco.server.service",))
            self.assertEqual(restart.call_args_list[1].args, ("com.edgedisco.agent.service",))
            verify.assert_called_once()
            self.assertEqual(result["asset_count"], 4)
            self.assertTrue(Path(result["cli"]).is_symlink())

    def test_linux_systemd_units_are_per_user_and_unprivileged(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = default_layout(home / ".edgedisco", home)
            cli = layout.venv / "bin/edgedisco"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\n")
            ensure_credentials(layout, 8765)
            server, agent = write_systemd_units(layout, 8765)
            self.assertEqual(server.parent, home / ".config/systemd/user")
            self.assertIn("NoNewPrivileges=true", server.read_text())
            self.assertIn("ProtectSystem=strict", server.read_text())
            self.assertIn("ReadWritePaths=", agent.read_text())
            self.assertIn("127.0.0.1 --port 8765", (layout.bin / "run-server.sh").read_text())
            self.assertIn("Requires=com.edgedisco.server.service", agent.read_text())
            self.assertNotIn("User=root", server.read_text() + agent.read_text())

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Linux")
    @patch("ai_asset_inventory.self_service._systemctl")
    def test_linux_uninstall_disables_user_units_and_preserves_data(self, systemctl, _system):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = default_layout(home / ".edgedisco", home)
            layout.root.mkdir()
            layout.systemd_user.mkdir(parents=True)
            for label in (SERVER_LABEL, AGENT_LABEL):
                (layout.systemd_user / f"{label}.service").write_text("unit")
            result = uninstall_linux(root=layout.root, home=home)
            self.assertFalse(any(layout.systemd_user.glob("com.edgedisco.*.service")))
            self.assertTrue(layout.root.exists())
            self.assertFalse(result["data_purged"])
            self.assertEqual(systemctl.call_count, 3)

    @patch("ai_asset_inventory.self_service._verify_browser_bootstrap")
    @patch("ai_asset_inventory.self_service._health", return_value={"assets": 1})
    def test_dashboard_opens_fresh_authenticated_session_without_exposing_token(self, _health, bootstrap):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".edgedisco"
            root.mkdir()
            (root / "server.env").write_text(
                "export AAI_ADMIN_TOKEN=private-admin-token\nexport EDGEDISCO_PORT=8765\n"
            )
            bootstrap.return_value = "http://127.0.0.1:8765/browser-bootstrap/one-time-code"
            browser = unittest.mock.Mock(return_value=True)
            self.assertEqual(open_dashboard(root, browser_fn=browser), "http://127.0.0.1:8765")
            _health.assert_called_once_with(8765, "private-admin-token")
            bootstrap.assert_called_once_with(8765, "private-admin-token")
            browser.assert_called_once_with(bootstrap.return_value)

    def test_dashboard_fails_closed_when_credential_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".edgedisco"
            root.mkdir()
            with self.assertRaisesRegex(RuntimeError, "Missing administrator credential"):
                open_dashboard(root)

    @patch("ai_asset_inventory.self_service.urllib.request.urlopen", side_effect=ConnectionResetError())
    def test_health_treats_shutdown_connection_reset_as_stopped(self, _urlopen):
        self.assertIsNone(_health(8090))

    @patch("ai_asset_inventory.self_service.time.monotonic", side_effect=[0, 11])
    @patch("ai_asset_inventory.self_service._health", return_value={"status": "ok"})
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_restart_rejects_stale_server_still_on_port(self, launchctl, _health, _clock):
        with self.assertRaisesRegex(RuntimeError, "stale server"):
            _restart_service(SERVER_LABEL, Path("/tmp/unused.plist"), wait_for_port=8090)
        self.assertEqual(launchctl.call_count, 1)
        self.assertEqual(launchctl.call_args.args[0], "bootout")

    @patch("ai_asset_inventory.self_service.urllib.request.urlopen")
    def test_browser_bootstrap_verification_uses_configured_port(self, urlopen):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"bootstrap_path":"/browser-bootstrap/test-code"}'
        urlopen.return_value = Response()
        for port in (8080, 8090, 49152):
            self.assertEqual(_verify_browser_bootstrap(port, "test-admin-token"),
                             f"http://127.0.0.1:{port}/browser-bootstrap/test-code")
            self.assertEqual(
                urlopen.call_args.args[0].full_url,
                f"http://127.0.0.1:{port}/api/v1/browser-bootstrap",
            )

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
    @patch("ai_asset_inventory.self_service._verify_browser_bootstrap")
    @patch("ai_asset_inventory.self_service.install_adapters", return_value=[])
    @patch("ai_asset_inventory.self_service.detect_adapters", return_value=["cursor"])
    @patch("ai_asset_inventory.self_service.AgentClient", FakeAgentClient)
    def test_setup_enrolls_and_sends_initial_inventory(
        self, _detect, _install, _verify, _wait, restart, _health, _system
    ):
        with tempfile.TemporaryDirectory() as temp:
            _verify.return_value = "http://127.0.0.1:8080/browser-bootstrap/fresh-code"
            with patch("ai_asset_inventory.self_service.webbrowser.open") as browser:
                result = setup_macos(
                    root=Path(temp) / ".edgedisco", home=Path(temp), open_dashboard=True
                )
                browser.assert_called_once_with(_verify.return_value)
                self.assertEqual(_verify.call_count, 2)
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

    @patch.dict(os.environ, {"SHELL": "/bin/bash"})
    def test_bash_login_profile_resolves_and_uninstalls_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout, _cli = self._layout_with_fake_cli(home)
            profile = home / ".bash_login"
            profile.write_text("export TEST_KEEP=1\n")
            launcher = install_cli_launcher(layout, home)
            self.assertEqual(launcher.path_profile, profile)
            self.assertFalse((home / ".zprofile").exists())
            env = dict(os.environ, HOME=str(home), PATH="/usr/local/bin:/usr/bin:/bin")
            resolved = subprocess.check_output(
                ["/bin/bash", "-lic", "command -v edgedisco"], env=env, text=True,
            ).strip()
            self.assertEqual(resolved, str(launcher.public))
            uninstall_cli_launcher(layout, home)
            self.assertEqual(profile.read_text(), "export TEST_KEEP=1\n")

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


class ServiceLifecycleTests(unittest.TestCase):
    def _create_macos_plists(self, home: Path, labels=(SERVER_LABEL, AGENT_LABEL)) -> Layout:
        layout = default_layout(home / ".edgedisco", home)
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.launch_agents.mkdir(parents=True, exist_ok=True)
        for label in labels:
            (layout.launch_agents / f"{label}.plist").write_bytes(b"plist-content")
        return layout

    def _create_linux_units(self, home: Path, labels=(SERVER_LABEL, AGENT_LABEL)) -> Layout:
        layout = default_layout(home / ".edgedisco", home)
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.systemd_user.mkdir(parents=True, exist_ok=True)
        for label in labels:
            (layout.systemd_user / f"{label}.service").write_text("unit-content")
        return layout

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._wait_for_server")
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_macos_start_all_services_in_forward_order(self, launchctl, wait_server, _system):
        def _fake_launchctl(action, *args, **kwargs):
            if action == "print":
                return subprocess.CompletedProcess(["launchctl"], returncode=1, stderr="Not found")
            return subprocess.CompletedProcess(["launchctl"], returncode=0, stdout="")

        launchctl.side_effect = _fake_launchctl
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home)
            result = start_macos(root=layout.root, home=home)
            self.assertEqual(result, {SERVER_LABEL: "started", AGENT_LABEL: "started"})
            bootstraps = [call.args[1] for call in launchctl.call_args_list if call.args[0] == "bootstrap"]
            self.assertEqual(bootstraps, [f"gui/{os.getuid()}", f"gui/{os.getuid()}"])

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._health", return_value={"status": "ok"})
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_macos_start_already_running(self, launchctl, _health, _system):
        launchctl.return_value = subprocess.CompletedProcess(["launchctl"], returncode=0, stdout="state = running")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home)
            result = start_macos(root=layout.root, home=home)
            self.assertEqual(result, {SERVER_LABEL: "already running", AGENT_LABEL: "already running"})

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._health", return_value=None)
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_macos_stop_all_services_in_reverse_order(self, launchctl, _health, _system):
        launchctl.return_value = subprocess.CompletedProcess(["launchctl"], returncode=0, stdout="state = running")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home)
            result = stop_macos(root=layout.root, home=home)
            self.assertEqual(result, {AGENT_LABEL: "stopped", SERVER_LABEL: "stopped"})
            bootouts = [call.args[1] for call in launchctl.call_args_list if call.args[0] == "bootout"]
            domain = f"gui/{os.getuid()}"
            self.assertEqual(bootouts, [f"{domain}/{AGENT_LABEL}", f"{domain}/{SERVER_LABEL}"])

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_macos_stop_already_stopped(self, launchctl, _system):
        launchctl.return_value = subprocess.CompletedProcess(["launchctl"], returncode=1, stderr="Not found")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home)
            result = stop_macos(root=layout.root, home=home)
            self.assertEqual(result, {AGENT_LABEL: "already stopped", SERVER_LABEL: "already stopped"})

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_macos_stop_failure_is_reported(self, launchctl, _system):
        def fake_launchctl(action, *args, **kwargs):
            if action == "print":
                return subprocess.CompletedProcess(["launchctl"], returncode=0, stdout="running")
            return subprocess.CompletedProcess(
                ["launchctl"], returncode=1, stderr="operation not permitted"
            )

        launchctl.side_effect = fake_launchctl
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home, labels=(AGENT_LABEL,))
            with self.assertRaisesRegex(RuntimeError, "could not stop"):
                stop_macos(root=layout.root, home=home)

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service.stop_macos")
    @patch("ai_asset_inventory.self_service.start_macos")
    def test_macos_restart_services(self, start_mac, stop_mac, _system):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home)
            result = restart_macos(root=layout.root, home=home)
            self.assertEqual(result, {SERVER_LABEL: "restarted", AGENT_LABEL: "restarted"})
            stop_mac.assert_called_once()
            start_mac.assert_called_once()

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Darwin")
    @patch("ai_asset_inventory.self_service._health", return_value=None)
    @patch("ai_asset_inventory.self_service._launchctl")
    def test_target_specific_service_by_alias(self, launchctl, _health, _system):
        launchctl.return_value = subprocess.CompletedProcess(["launchctl"], returncode=0, stdout="state = running")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_macos_plists(home)
            stop_res = stop_macos(root=layout.root, services=["agent"], home=home)
            self.assertEqual(stop_res, {AGENT_LABEL: "stopped"})

            def _fake_launchctl(action, *args, **kwargs):
                if action == "print":
                    return subprocess.CompletedProcess(["launchctl"], returncode=1, stderr="Not found")
                return subprocess.CompletedProcess(["launchctl"], returncode=0, stdout="")

            launchctl.side_effect = _fake_launchctl
            start_res = start_macos(root=layout.root, services=["server"], home=home)
            self.assertEqual(start_res, {SERVER_LABEL: "started"})

    def test_service_validation_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = default_layout(home / ".edgedisco", home)
            with self.assertRaisesRegex(RuntimeError, "No EdgeDisco services found"):
                start_macos(root=layout.root, home=home)

            layout.launch_agents.mkdir(parents=True, exist_ok=True)
            (layout.launch_agents / f"{SERVER_LABEL}.plist").write_bytes(b"data")
            with self.assertRaisesRegex(RuntimeError, "Unknown service"):
                start_macos(root=layout.root, services=["nonexistent"], home=home)

            with self.assertRaisesRegex(RuntimeError, "Service com.edgedisco.otlp-export is not installed"):
                start_macos(root=layout.root, services=["otlp-export"], home=home)

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Linux")
    @patch("ai_asset_inventory.self_service.require_systemd_user")
    @patch("ai_asset_inventory.self_service._systemctl")
    def test_linux_start_stop_restart(self, systemctl, require_sd, _system):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_linux_units(home)
            systemctl.return_value = subprocess.CompletedProcess(["systemctl"], returncode=0, stdout="inactive")

            start_res = start_linux(root=layout.root, home=home)
            self.assertEqual(start_res, {SERVER_LABEL: "started", AGENT_LABEL: "started"})

            systemctl.return_value = subprocess.CompletedProcess(["systemctl"], returncode=0, stdout="active")
            stop_res = stop_linux(root=layout.root, home=home)
            self.assertEqual(stop_res, {AGENT_LABEL: "stopped", SERVER_LABEL: "stopped"})

            restart_res = restart_linux(root=layout.root, home=home)
            self.assertEqual(restart_res, {SERVER_LABEL: "restarted", AGENT_LABEL: "restarted"})

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Linux")
    @patch("ai_asset_inventory.self_service.require_systemd_user")
    @patch("ai_asset_inventory.self_service._systemctl")
    def test_linux_stop_failure_is_reported(self, systemctl, require_sd, _system):
        def fake_systemctl(action, *args, **kwargs):
            if action == "is-active":
                return subprocess.CompletedProcess(["systemctl"], returncode=0, stdout="active")
            if action == "stop":
                return subprocess.CompletedProcess(
                    ["systemctl"], returncode=1, stderr="permission denied"
                )
            return subprocess.CompletedProcess(["systemctl"], returncode=0, stdout="")

        systemctl.side_effect = fake_systemctl
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = self._create_linux_units(home, labels=(SERVER_LABEL,))
            with self.assertRaisesRegex(RuntimeError, "could not stop"):
                stop_linux(root=layout.root, home=home)

    @patch("ai_asset_inventory.self_service.platform.system", return_value="Windows")
    def test_unsupported_os_raises(self, _system):
        with self.assertRaisesRegex(RuntimeError, "supports macOS and systemd-based Linux"):
            start_services()
        with self.assertRaisesRegex(RuntimeError, "supports macOS and systemd-based Linux"):
            stop_services()
        with self.assertRaisesRegex(RuntimeError, "supports macOS and systemd-based Linux"):
            restart_services()

    @patch("ai_asset_inventory.cli.start_services", return_value={SERVER_LABEL: "started"})
    @patch("ai_asset_inventory.cli.stop_services", return_value={SERVER_LABEL: "stopped"})
    @patch("ai_asset_inventory.cli.restart_services", return_value={SERVER_LABEL: "restarted"})
    def test_cli_lifecycle_commands(self, restart_mock, stop_mock, start_mock):
        from ai_asset_inventory.cli import main
        out = io.StringIO()
        with patch("sys.argv", ["edgedisco", "start", "server"]), redirect_stdout(out):
            main()
        start_mock.assert_called_once_with(root=None, services=["server"])
        self.assertIn(f"{SERVER_LABEL}: started", out.getvalue())

        out = io.StringIO()
        with patch("sys.argv", ["edgedisco", "stop"]), redirect_stdout(out):
            main()
        stop_mock.assert_called_once_with(root=None, services=None)
        self.assertIn(f"{SERVER_LABEL}: stopped", out.getvalue())

        out = io.StringIO()
        with patch("sys.argv", ["edgedisco", "restart", "agent"]), redirect_stdout(out):
            main()
        restart_mock.assert_called_once_with(root=None, services=["agent"])
        self.assertIn(f"{SERVER_LABEL}: restarted", out.getvalue())


if __name__ == "__main__":
    unittest.main()
