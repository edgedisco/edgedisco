"""Installer regressions, including bash -c with no BASH_SOURCE path."""

from __future__ import annotations

import os
import platform
import json
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request
import urllib.error
import http.cookiejar
from pathlib import Path
from ai_asset_inventory.database import Database


REPO = Path(__file__).resolve().parents[1]
SCRIPT = (REPO / "install.sh").read_text()
README = (REPO / "README.md").read_text()
COMMANDS = ("server", "agent", "hook", "adapters", "setup", "status", "dashboard",
            "uninstall", "mcp", "demo")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InstallerTests(unittest.TestCase):
    def test_readme_documents_streamed_and_downloaded_installers(self):
        self.assertIn(
            "curl -fsSL https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh | bash",
            README,
        )
        self.assertIn(
            "curl -fsSLO https://raw.githubusercontent.com/edgedisco/edgedisco/main/install.sh\n"
            "less install.sh\n"
            "bash install.sh",
            README,
        )

    def test_setup_cannot_consume_piped_installer_input(self):
        self.assertIn('"$CLI" setup "${SETUP_ARGS[@]}" </dev/null', SCRIPT)

    def test_stdin_install_without_terminal_requires_yes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            uname = fake_bin / "uname"
            uname.write_text("#!/bin/sh\necho Darwin\n")
            uname.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", "-s"],
                env=dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(home / ".edgedisco"),
                         PYTHON_BIN=sys.executable,
                         PATH=f"{fake_bin}:{os.environ.get('PATH', '')}"),
                input=SCRIPT, capture_output=True, text=True, timeout=10,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rerun with --yes", result.stderr)
        self.assertFalse((home / ".edgedisco").exists())

    def test_help_lists_every_option_and_override_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            env = dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(home / ".edgedisco"))
            for command in (["/bin/bash", str(REPO / "install.sh"), "--help"],
                            ["/bin/bash", "-c", SCRIPT, "--", "-h"]):
                with self.subTest(command=command):
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for value in ("--help", "--yes", "--port PORT", "--all-adapters", "--no-open",
                                  "EDGEDISCO_HOME", "PYTHON_BIN", "EDGEDISCO_ARCHIVE_URL"):
                        self.assertIn(value, result.stdout)
                    self.assertFalse((home / ".edgedisco").exists())

    def test_unknown_option_shows_help_and_fails_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = subprocess.run(
                ["/bin/bash", str(REPO / "install.sh"), "--unknown"],
                env=dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(home / ".edgedisco")),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Unknown option: --unknown", result.stderr)
            self.assertIn("Usage: bash install.sh [options]", result.stderr)
            self.assertFalse((home / ".edgedisco").exists())

    def test_linux_requires_an_active_systemd_user_session_before_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            for name, body in (
                ("uname", "#!/bin/sh\necho Linux\n"),
                ("systemctl", "#!/bin/sh\nexit 1\n"),
            ):
                path = fake_bin / name
                path.write_text(body)
                path.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(REPO / "install.sh"), "--yes", "--no-open"],
                env=dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(home / ".edgedisco"),
                         PATH=f"{fake_bin}:/usr/bin:/bin"),
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active systemd user session", result.stderr)
            self.assertFalse((home / ".edgedisco").exists())

    def test_remote_stdin_failure_is_immediate_and_does_not_create_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            uname = fake_bin / "uname"
            uname.write_text("#!/bin/sh\necho Darwin\n")
            uname.chmod(0o755)
            env = dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(home / ".edgedisco"),
                       EDGEDISCO_ARCHIVE_URL="file:///nonexistent/edgedisco.tar.gz",
                       PYTHON_BIN=sys.executable,
                       PATH=f"{fake_bin}:{os.environ.get('PATH', '')}")
            result = subprocess.run(
                ["/bin/bash", "-c", SCRIPT, "--", "--yes", "--no-open"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Downloading EdgeDisco source archive", result.stdout)
            self.assertIn("installation failed", result.stderr)
            self.assertNotIn("BASH_SOURCE", result.stderr)
            self.assertNotIn("installation verified", result.stdout)
            self.assertFalse((home / ".edgedisco/venv").exists())
            self.assertFalse((home / ".local/bin/edgedisco").exists())

    @unittest.skipUnless(platform.system() == "Darwin", "macOS LaunchAgent integration")
    def test_remote_active_venv_partial_repair_local_rerun_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / ".edgedisco"
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            state = home / "fake-launchctl-state"
            state.mkdir()
            launchctl = fake_bin / "launchctl"
            launchctl.write_text(
                f"#!{sys.executable}\n"
                "import os, pathlib, plistlib, signal, subprocess, sys\n"
                "state = pathlib.Path(os.environ['FAKE_LAUNCHCTL_STATE'])\n"
                "action = sys.argv[1]\n"
                "if action == 'bootout':\n"
                "    name = sys.argv[2].rsplit('/', 1)[-1]\n"
                "    pidfile = state / name\n"
                "    if pidfile.exists():\n"
                "        try: os.kill(int(pidfile.read_text()), signal.SIGTERM)\n"
                "        except ProcessLookupError: pass\n"
                "        pidfile.unlink()\n"
                "elif action == 'bootstrap':\n"
                "    data = plistlib.loads(pathlib.Path(sys.argv[3]).read_bytes())\n"
                "    if data['Label'].endswith('.server'):\n"
                "        out = open(data['StandardOutPath'], 'ab')\n"
                "        err = open(data['StandardErrorPath'], 'ab')\n"
                "        proc = subprocess.Popen(data['ProgramArguments'], stdout=out, stderr=err, start_new_session=True)\n"
                "        (state / data['Label']).write_text(str(proc.pid))\n"
            )
            launchctl.chmod(0o755)
            git = fake_bin / "git"
            git.write_text("#!/bin/sh\nexit 99\n")
            git.chmod(0o755)
            browser = fake_bin / "demo-browser"
            browser.write_text("#!/bin/sh\nprintf '%s' \"$1\" > \"$EDGEDISCO_BROWSER_URL_FILE\"\n")
            browser.chmod(0o755)
            browser_url_file = home / "browser-url"

            unrelated = home / "unrelated-venv"
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(unrelated)], check=True)
            old_cli = unrelated / "bin/edgedisco"
            old_cli.write_text("#!/bin/sh\necho old-edgedisco\n")
            old_cli.chmod(0o755)
            old_public = home / ".local/bin/edgedisco"
            old_public.parent.mkdir(parents=True)
            old_public.write_text("#!/bin/sh\necho another-old-edgedisco\n")
            old_public.chmod(0o755)
            sentinel = unrelated / "KEEP"
            sentinel.write_text("untouched")
            root.mkdir()
            evidence = root / "existing-evidence.keep"
            evidence.write_text("preserve")
            old_managed = root / "bin/edgedisco"
            old_managed.parent.mkdir()
            old_managed.write_text("#!/bin/sh\necho old-managed-edgedisco\n")
            old_managed.chmod(0o755)
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], check=True)

            archive = home / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in ("pyproject.toml", "README.md"):
                    tar.add(REPO / name, arcname=f"edgedisco-main/{name}")
                for path in (REPO / "src/ai_asset_inventory").rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        tar.add(path, arcname=f"edgedisco-main/{path.relative_to(REPO)}")
            port = _free_port()
            admin_token = "existing-admin-token-for-upgrade-test"
            enrollment_token = "existing-enrollment-token-for-upgrade-test"
            (root / "server.env").write_text(
                f"export AAI_ADMIN_TOKEN={admin_token}\n"
                f"export AAI_ENROLLMENT_TOKEN={enrollment_token}\n"
                f"EDGEDISCO_PORT={port}\n"
            )
            old_database = Database(root / "data/inventory.db")
            old_device, _ = old_database.enroll({"hostname": "existing-host", "os": "Darwin"})
            old_database.ingest(old_device, {
                "scan_id": "existing-scan", "observed_at": "2026-09-19T00:00:00+00:00",
                "assets": [{"fingerprint": "existing-evidence", "kind": "application",
                            "name": "Existing EdgeDisco Evidence", "vendor": "Test", "running": False}],
                "privacy": {"content_captured": False},
            })
            env = dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(root),
                       EDGEDISCO_ARCHIVE_URL=archive.as_uri(),
                       VIRTUAL_ENV=str(unrelated),
                       FAKE_LAUNCHCTL_STATE=str(state),
                       BROWSER=str(browser),
                       EDGEDISCO_BROWSER_URL_FILE=str(browser_url_file),
                       PATH=f"{unrelated / 'bin'}:{fake_bin}:{os.environ.get('PATH', '')}")
            env.pop("PYTHON_BIN", None)
            env.pop("PYTHONPATH", None)
            # An already-running pre-bootstrap server must be stopped before
            # setup can accept the upgraded server as healthy.
            old_code = (
                "import os, sys\n"
                "from pathlib import Path\n"
                "from ai_asset_inventory.database import Database\n"
                "from ai_asset_inventory.server import InventoryServer, RequestHandler\n"
                "original = RequestHandler.do_POST\n"
                "def legacy(self):\n"
                "    if self.path == '/api/v1/browser-bootstrap':\n"
                "        return self._json(404, {'error': 'not found'})\n"
                "    return original(self)\n"
                "RequestHandler.do_POST = legacy\n"
                "InventoryServer(('127.0.0.1', int(sys.argv[1])), Database(Path(sys.argv[2])), "
                "os.environ['AAI_ADMIN_TOKEN'], os.environ['AAI_ENROLLMENT_TOKEN']).serve_forever()\n"
            )
            old_env = dict(env, PYTHONPATH=str(REPO / "src"),
                           AAI_ADMIN_TOKEN=admin_token, AAI_ENROLLMENT_TOKEN=enrollment_token)
            old_server = subprocess.Popen(
                [sys.executable, "-c", old_code, str(port), str(root / "data/inventory.db")],
                env=old_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                for _ in range(50):
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2).close()
                        break
                    except urllib.error.URLError:
                        time.sleep(0.1)
                self.assertIsNone(old_server.poll())
                old_probe = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/browser-bootstrap", data=b"",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as old_error:
                    urllib.request.urlopen(old_probe)
                self.assertEqual(old_error.exception.code, 404)

                stale = subprocess.run(
                    ["/bin/bash", "-c", SCRIPT, "--", "--yes", "--no-open"],
                    env=env, capture_output=True, text=True, timeout=180,
                )
                self.assertNotEqual(stale.returncode, 0)
                self.assertIn("stale server", stale.stderr)
                self.assertNotIn("EdgeDisco installation verified.", stale.stdout)
                self.assertIsNone(old_server.poll())

                (state / "com.edgedisco.server").write_text(str(old_server.pid))

                remote = subprocess.run(
                    ["/bin/bash", "-c", SCRIPT], env=env, input="y\n",
                    capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(remote.returncode, 0, remote.stderr)
                self.assertIn("EdgeDisco installation verified.", remote.stdout)
                old_server.wait(timeout=5)
                with urllib.request.urlopen(old_probe, timeout=5) as upgraded:
                    self.assertEqual(upgraded.status, 201)
                with urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/summary",
                    headers={"Authorization": f"Bearer {admin_token}"},
                ), timeout=5) as response:
                    upgraded_inventory = json.load(response)
                self.assertIn("Existing EdgeDisco Evidence",
                              {row["name"] for row in upgraded_inventory["items"]})
                self.assertIn("Another edgedisco command is currently active from:", remote.stdout)
                self.assertIn(str(old_cli), remote.stdout)
                self.assertIn(str(root / "bin/edgedisco") + " demo", remote.stdout)
                self.assertIn("Open a new Terminal", remote.stdout)
                self.assertNotIn(admin_token, remote.stdout + remote.stderr)
                self.assertNotIn(enrollment_token, remote.stdout + remote.stderr)
                self.assertNotIn("VIRTUAL_ENV", remote.stdout + remote.stderr)
                self.assertEqual(sentinel.read_text(), "untouched")
                self.assertEqual(old_cli.read_text(), "#!/bin/sh\necho old-edgedisco\n")
                self.assertEqual(evidence.read_text(), "preserve")
                managed = root / "venv/bin/edgedisco"
                help_text = subprocess.check_output([str(managed), "--help"], env=env, text=True)
                for command in COMMANDS:
                    self.assertIn(command, help_text)
                source = subprocess.check_output(
                    [str(root / "venv/bin/python"), "-c",
                     "import ai_asset_inventory; print(ai_asset_inventory.__file__)"],
                    env=env, text=True,
                ).strip()
                self.assertTrue(Path(source).resolve().is_relative_to((root / "venv").resolve()), source)
                unrelated_package = subprocess.check_output(
                    [str(unrelated / "bin/python"), "-c",
                     "import importlib.util; print(importlib.util.find_spec('ai_asset_inventory'))"],
                    env=env, text=True,
                ).strip()
                self.assertEqual(unrelated_package, "None")
                self.assertEqual((root / "cli-launcher.path").read_text().strip(),
                                 str(root / "bin/edgedisco"))
                self.assertEqual(old_public.read_text(), "#!/bin/sh\necho another-old-edgedisco\n")
                self.assertTrue((root / "bin/edgedisco").is_file())
                self.assertTrue(os.access(root / "bin/edgedisco", os.X_OK))
                self.assertNotIn("old-managed-edgedisco", old_managed.read_text())
                self.assertIn("exec ", old_managed.read_text())
                values = (root / "server.env").read_text()
                self.assertIn(f"AAI_ADMIN_TOKEN={admin_token}", values)
                self.assertIn(f"AAI_ENROLLMENT_TOKEN={enrollment_token}", values)
                config = json.loads((root / "agent.json").read_text())
                device_token = config["device_token"]

                # A same-version rerun must replace package data from the new
                # source instead of letting pip report it as already satisfied.
                installed_catalog = Path(source).parent / "fingerprints.json"
                expected_catalog = (REPO / "src/ai_asset_inventory/fingerprints.json").read_text()
                installed_catalog.write_text("stale-test-catalog")
                self.assertIn("stale-test-catalog", installed_catalog.read_text())

                # Verification must not source arbitrary shell startup files.
                # A user alias can shadow PATH later, but must not be executed by
                # an installer rerun or make the managed installation fail.
                (home / ".zshrc").write_text("alias edgedisco='echo old-shadow'\n")
                shadowed = subprocess.run(
                    ["/bin/bash", "-c", SCRIPT, "--", "--yes", "--no-open"],
                    env=env, capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(shadowed.returncode, 0, shadowed.stdout + shadowed.stderr)
                self.assertIn("EdgeDisco installation verified.", shadowed.stdout)
                self.assertNotIn("old-shadow", shadowed.stdout + shadowed.stderr)
                self.assertEqual(installed_catalog.read_text(), expected_catalog)
                (home / ".zshrc").unlink()

                demo = subprocess.run([str(managed), "demo"], env=env, capture_output=True,
                                      text=True, timeout=40)
                self.assertEqual(demo.returncode, 0, demo.stdout + demo.stderr)
                self.assertIn("4 AI runtime families discovered", demo.stdout)
                self.assertNotIn("manual admin-token", demo.stdout)
                browser_url = browser_url_file.read_text()
                self.assertTrue(browser_url.startswith(f"http://127.0.0.1:{port}/browser-bootstrap/"))
                jar = http.cookiejar.CookieJar()
                browser_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
                with browser_client.open(browser_url) as response:
                    self.assertEqual(response.url, f"http://127.0.0.1:{port}/")
                    self.assertIn(b"DEMO LAB", response.read())
                with browser_client.open(f"http://127.0.0.1:{port}/api/v1/summary") as response:
                    browser_summary = json.load(response)
                self.assertEqual({row["name"] for row in browser_summary["items"]
                                  if row["metadata"].get("demo_lab")},
                                 {"CrewAI", "AutoGen", "LangGraph/LangChain", "MCP Server"})
                token = admin_token
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/summary",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    summary = json.load(response)
                rows = [row for row in summary["items"] if row["metadata"].get("demo_lab")]
                self.assertEqual({row["name"] for row in rows},
                                 {"CrewAI", "AutoGen", "LangGraph/LangChain", "MCP Server"})
                self.assertTrue(all(not row["running"] for row in rows))

                no_collision_env = dict(env, PATH=f"{root / 'bin'}:{fake_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
                no_collision_env.pop("VIRTUAL_ENV", None)
                local = subprocess.run(
                    ["/bin/bash", str(REPO / "install.sh"), "--yes", "--no-open", "--port", str(port)],
                    env=no_collision_env, capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(local.returncode, 0, local.stderr)
                self.assertIn("EdgeDisco installation verified.", local.stdout)
                self.assertIn(f"Using local EdgeDisco source checkout: {REPO}", local.stdout)
                self.assertNotIn("Downloading EdgeDisco source archive", local.stdout)
                self.assertNotIn("Another edgedisco command", local.stdout)
                self.assertEqual(evidence.read_text(), "preserve")
                self.assertIn(f"AAI_ADMIN_TOKEN={admin_token}", (root / "server.env").read_text())
                self.assertIn(f"AAI_ENROLLMENT_TOKEN={enrollment_token}", (root / "server.env").read_text())
                self.assertEqual(json.loads((root / "agent.json").read_text())["device_token"], device_token)
                self.assertEqual((home / ".zprofile").read_text().count("# >>> edgedisco PATH >>>"), 1)
                with urllib.request.urlopen(request, timeout=5) as response:
                    upgraded_summary = json.load(response)
                self.assertEqual({row["name"] for row in upgraded_summary["items"]
                                  if row["metadata"].get("demo_lab")},
                                 {"CrewAI", "AutoGen", "LangGraph/LangChain", "MCP Server"})

                # Fail after replacing the package, and verify real rollback
                # restores the prior runnable venv, credentials, and database.
                config_before_failure = (root / "agent.json").read_bytes()
                broken_script = SCRIPT.replace('SETUP_ARGS=(--root "$INSTALL_ROOT")', 'SETUP_ARGS=()')
                self.assertNotEqual(broken_script, SCRIPT)
                nounset = subprocess.run(
                    ["/bin/bash", "-c", broken_script], env=env, input="y\n",
                    capture_output=True, text=True, timeout=180,
                )
                self.assertNotEqual(nounset.returncode, 0)
                self.assertIn("SETUP_ARGS[@]: unbound variable", nounset.stderr)
                self.assertIn("Previous installation restored", nounset.stderr)
                self.assertEqual((root / "agent.json").read_bytes(), config_before_failure)
                self.assertIn("demo", subprocess.check_output([str(managed), "--help"], env=env, text=True))
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(request, timeout=0.5) as response:
                            restored = json.load(response)
                        break
                    except urllib.error.URLError:
                        time.sleep(0.1)
                else:
                    self.fail("Rolled-back server did not become healthy")
                self.assertEqual(restored["devices"], upgraded_summary["devices"])
                self.assertEqual(restored["assets"], upgraded_summary["assets"])

                removed = subprocess.run([str(managed), "uninstall", "--root", str(root)],
                                         env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertEqual(old_public.read_text(), "#!/bin/sh\necho another-old-edgedisco\n")
                self.assertFalse((root / "bin/edgedisco").exists())
                self.assertEqual(evidence.read_text(), "preserve")
                self.assertEqual(sentinel.read_text(), "untouched")
            finally:
                if old_server.poll() is None:
                    old_server.terminate()
                    old_server.wait(timeout=5)
                pidfile = state / "com.edgedisco.server"
                if pidfile.exists():
                    import signal
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
