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
import unittest
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = (REPO / "install.sh").read_text()
COMMANDS = ("server", "agent", "hook", "adapters", "setup", "status",
            "uninstall", "mcp", "demo")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InstallerTests(unittest.TestCase):
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
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], check=True)

            archive = home / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in ("pyproject.toml", "README.md"):
                    tar.add(REPO / name, arcname=f"edgedisco-main/{name}")
                for path in (REPO / "src/ai_asset_inventory").rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        tar.add(path, arcname=f"edgedisco-main/{path.relative_to(REPO)}")
            port = _free_port()
            env = dict(os.environ, HOME=str(home), EDGEDISCO_HOME=str(root),
                       EDGEDISCO_ARCHIVE_URL=archive.as_uri(),
                       VIRTUAL_ENV=str(unrelated),
                       FAKE_LAUNCHCTL_STATE=str(state),
                       BROWSER="/usr/bin/true",
                       PATH=f"{unrelated / 'bin'}:{fake_bin}:{os.environ.get('PATH', '')}")
            env.pop("PYTHON_BIN", None)
            env.pop("PYTHONPATH", None)
            try:
                remote = subprocess.run(
                    ["/bin/bash", "-c", SCRIPT, "--", "--yes", "--no-open", "--port", str(port)],
                    env=env, capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(remote.returncode, 0, remote.stderr)
                self.assertIn("installation verified", remote.stdout)
                self.assertIn("Bootstrapping pip", remote.stdout)
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

                demo = subprocess.run([str(managed), "demo"], env=env, capture_output=True,
                                      text=True, timeout=40)
                self.assertEqual(demo.returncode, 0, demo.stdout + demo.stderr)
                self.assertIn("4 AI runtime families discovered", demo.stdout)
                token = next(line.split("=", 1)[1] for line in (root / "server.env").read_text().splitlines()
                             if line.startswith("export AAI_ADMIN_TOKEN="))
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

                local = subprocess.run(
                    ["/bin/bash", str(REPO / "install.sh"), "--yes", "--no-open", "--port", str(port)],
                    env=env, capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(local.returncode, 0, local.stderr)
                self.assertEqual(evidence.read_text(), "preserve")
                self.assertEqual((home / ".zprofile").read_text().count("# >>> edgedisco PATH >>>"), 1)

                removed = subprocess.run([str(managed), "uninstall", "--root", str(root)],
                                         env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertEqual(old_public.read_text(), "#!/bin/sh\necho another-old-edgedisco\n")
                self.assertFalse((root / "bin/edgedisco").exists())
                self.assertEqual(evidence.read_text(), "preserve")
                self.assertEqual(sentinel.read_text(), "untouched")
            finally:
                pidfile = state / "com.edgedisco.server"
                if pidfile.exists():
                    import signal
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
