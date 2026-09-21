import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory import detector


HOMEBREW_FRAMEWORK_PYTHON = (
    "/opt/homebrew/Cellar/python@3.14/3.14.4/Frameworks/Python.framework/"
    "Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
)


class DetectorTests(unittest.TestCase):
    def test_incremental_scanner_refreshes_processes_but_reuses_static_evidence(self):
        static = detector.Asset(
            fingerprint="a" * 64, kind="application", name="Claude", vendor="Anthropic",
            running=False,
        )
        process = detector.Asset(
            fingerprint="b" * 64, kind="process", name="Claude", vendor="Anthropic", running=True,
        )
        clock = unittest.mock.Mock(side_effect=[0.0, 10.0])
        scanner = detector.InventoryScanner(static_refresh_seconds=900, clock=clock)
        with patch.object(detector, "_static_revision", return_value=(("root", 1, 1, 1, 1),)), \
             patch.object(detector, "scan_static_inventory", return_value=[static]) as scan_static, \
             patch.object(detector, "scan_processes", return_value=[process]) as scan_processes:
            first = scanner.collect_inventory()
            second = scanner.collect_inventory()
        self.assertEqual(first, second)
        scan_static.assert_called_once_with()
        self.assertEqual(scan_processes.call_count, 2)

    def test_incremental_scanner_invalidates_on_metadata_change_or_ttl(self):
        scanner = detector.InventoryScanner(
            static_refresh_seconds=60,
            clock=unittest.mock.Mock(side_effect=[0.0, 10.0, 71.0]),
        )
        revisions = (
            (("root", 1, 1, 1, 1),),
            (("root", 1, 2, 2, 2),),
            (("root", 1, 2, 2, 2),),
        )
        with patch.object(detector, "_static_revision", side_effect=revisions), \
             patch.object(detector, "scan_static_inventory", return_value=[]) as scan_static, \
             patch.object(detector, "scan_processes", return_value=[]):
            scanner.collect_inventory()
            scanner.collect_inventory()
            scanner.collect_inventory()
        self.assertEqual(scan_static.call_count, 3)

    def test_process_inventory_asks_ps_for_current_unix_user_only(self):
        output = (
            "10 1 /usr/local/bin/codex codex\n"
            "11 1 /usr/local/bin/claude claude\n"
        )
        completed = unittest.mock.Mock(stdout=output)
        with patch.object(detector.platform, "system", return_value="Darwin"), \
             patch.object(detector.subprocess, "run", return_value=completed) as run:
            rows = detector._process_rows()
        self.assertEqual([row.pid for row in rows], [10, 11])
        self.assertEqual(run.call_args.args[0], [
            "ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,comm=,args=",
        ])

    def test_windows_process_query_filters_by_owner(self):
        completed = unittest.mock.Mock(stdout=json.dumps({
            "ProcessId": 10,
            "ParentProcessId": 1,
            "Name": "codex.exe",
            "ExecutablePath": "C:\\Users\\tester\\bin\\codex.exe",
            "CommandLine": "codex.exe",
        }))
        with patch.object(detector.platform, "system", return_value="Windows"), \
             patch.object(detector.subprocess, "run", return_value=completed) as run:
            rows = detector._process_rows()
        self.assertEqual([row.pid for row in rows], [10])
        self.assertEqual(rows[0].executable, "C:\\Users\\tester\\bin\\codex.exe")
        self.assertIn("GetOwner", run.call_args.args[0][-1])
        self.assertIn("$env:USERNAME", run.call_args.args[0][-1])
        self.assertIn("ExecutablePath", run.call_args.args[0][-1])

    def test_supported_agent_cli_processes_are_agent_runtimes(self):
        rows = [
            detector.ProcessObservation(index, 1, f"/usr/local/bin/{executable}", [executable])
            for index, (_name, _vendor, executables, _markers) in
            enumerate(detector.AGENT_CLI_SIGNATURES, 10)
            for executable in executables[:1]
        ]
        with patch.object(detector, "_process_rows", return_value=rows), \
             patch.object(detector, "_binary_evidence", return_value=("a" * 64, "matched")):
            assets = detector.scan_processes()
        runtimes = {asset.name for asset in assets if asset.kind == "agent_runtime"}
        self.assertEqual(runtimes, {item[0] for item in detector.AGENT_CLI_SIGNATURES})
        self.assertTrue(all(asset.metadata["host_app"] == "Direct/local"
                            for asset in assets if asset.kind == "agent_runtime"))

    def test_generic_runtime_uses_package_identity_not_prompt_text(self):
        codex = detector.ProcessObservation(
            10, 1, "/usr/bin/node", ["node /opt/node_modules/@openai/codex/bin/codex.js --prompt private"]
        )
        unrelated = detector.ProcessObservation(
            11, 1, "/usr/bin/node", ["node /srv/app.js please run @openai/codex"]
        )
        python_prompt = detector.ProcessObservation(
            12, 1, "/usr/bin/python3", ["python3 /srv/app.py --prompt 'please use crewai'"]
        )
        python_code = detector.ProcessObservation(
            13, 1, "/usr/bin/python3", ["python3 -c \"print('crewai')\""]
        )
        self.assertEqual(detector._classify_process(codex), ("OpenAI Codex", "OpenAI"))
        self.assertIsNone(detector._classify_process(unrelated))
        self.assertIsNone(detector._classify_process(python_prompt))
        self.assertIsNone(detector._classify_process(python_code))

    def test_interpreter_launched_agent_entry_point_is_detected(self):
        row = detector.ProcessObservation(
            10, 1, "/usr/bin/python3", ["/private/venv/bin/aider --message private"]
        )
        self.assertEqual(detector._classify_process(row), ("Aider", "Aider"))

    def test_hermes_and_openclaw_process_identities_are_detected(self):
        cases = [
            (
                detector.ProcessObservation(10, 1, "/home/user/.local/bin/hermes", ["hermes"]),
                ("Hermes Agent", "Nous Research"),
            ),
            (
                detector.ProcessObservation(
                    11,
                    1,
                    "/usr/bin/node",
                    ["node /opt/node_modules/openclaw/dist/index.js gateway"],
                ),
                ("OpenClaw", "OpenClaw"),
            ),
        ]
        for row, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(detector._classify_process(row), expected)

    def test_hermes_and_openclaw_mentions_in_prompt_text_are_ignored(self):
        for prompt in ("please use hermes-agent", "please use openclaw"):
            with self.subTest(prompt=prompt):
                row = detector.ProcessObservation(
                    12, 1, "/usr/bin/python3", [f"python3 /srv/app.py --prompt '{prompt}'"]
                )
                self.assertIsNone(detector._classify_process(row))

    def test_new_agent_package_and_console_script_identities(self):
        self.assertIsNone(detector._classify_process(detector.ProcessObservation(
            42, 1, "/usr/bin/docker", ["docker run unrelated/crush"])))
        cases = [
            ("node", "/opt/node_modules/@moonshot-ai/kimi-code/bin/cli.js", "Kimi Code"),
            ("python3", "-m kimi_cli", "Kimi Code"),
            ("node", "/opt/node_modules/@kilocode/cli/bin/kilo.js", "Kilo Code"),
            ("python3", "/opt/venv/bin/vibe-acp", "Mistral Vibe"),
            ("node", "/opt/node_modules/@charmland/crush/bin/crush.js", "Crush"),
            ("node", "/opt/node_modules/@augmentcode/auggie/bin/cli.js", "Auggie"),
            ("python3", "/opt/bin/junie", "Junie CLI"),
            ("python3", "/opt/bin/devin", "Devin CLI"),
        ]
        for runtime, entrypoint, name in cases:
            with self.subTest(name=name, entrypoint=entrypoint):
                row = detector.ProcessObservation(42, 1, f"/usr/bin/{runtime}",
                    [f"{runtime} {entrypoint} --prompt secret"])
                self.assertEqual(detector._classify_process(row)[0], name)
                unrelated = detector.ProcessObservation(42, 1, f"/usr/bin/{runtime}",
                    [f"{runtime} /srv/app.py --prompt '{entrypoint}'"])
                self.assertIsNone(detector._classify_process(unrelated))

    def test_package_runner_identity_is_exact_and_version_aware(self):
        cases = [
            ("npx @augmentcode/auggie", "Auggie"),
            ("npx @augmentcode/auggie@latest", "Auggie"),
            ("npx @kilocode/cli", "Kilo Code"),
            ("npx @kilocode/cli@1.2.3", "Kilo Code"),
            ("npx @moonshot-ai/kimi-code", "Kimi Code"),
            ("uvx mistral-vibe==2.0", "Mistral Vibe"),
            ("npx @unrelated/auggie", None),
            ("npx @unrelated/crush", None),
            ("npx @kilocode/cli-helper", None),
            ("python3 -m kimi_cli_helper", None),
            ("python3 /srv/hermes-agent-tests/worker.py", None),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                runtime = command.split()[0]
                row = detector.ProcessObservation(42, 1, f"/usr/bin/{runtime}", [command])
                classified = detector._classify_process(row)
                self.assertEqual(classified[0] if classified else None, expected)

    def test_windows_droid_uses_executable_path_for_hash_verification(self):
        row = detector.ProcessObservation(
            42, 1, "C:\\Users\\tester\\.local\\bin\\droid.exe", ["droid.exe"]
        )
        with patch.object(detector.platform, "system", return_value="Windows"), \
             patch.object(detector, "_binary_evidence", return_value=("a" * 64, "matched")) as evidence:
            self.assertEqual(detector._classify_process(row), ("Factory Droid", "Factory"))
        evidence.assert_called_once_with("Factory Droid", Path(row.executable))

    def test_droid_requires_published_hash_for_process_and_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            binary = root / "droid"
            binary.write_bytes(b"unrelated program named droid")
            row = detector.ProcessObservation(42, 1, str(binary), [str(binary)])
            with patch.object(detector, "_executable_roots", return_value=(root,)), \
                 patch.object(detector, "_hash_roots", return_value=(root,)):
                self.assertIsNone(detector._classify_process(row))
                self.assertEqual(list(detector._installed_cli_candidates()), [])
                factory = next(e for e in detector.AGENT_FINGERPRINTS if e.name == "Factory Droid")
                with patch.object(detector, "sha256_file", return_value=factory.binary_fingerprints[0].sha256):
                    self.assertEqual(detector._classify_process(row), ("Factory Droid", "Factory"))
                    assets = detector.scan_installed_clis()
                    self.assertEqual([a.name for a in assets], ["Factory Droid"])
                    self.assertEqual(assets[0].binary_fingerprint_status, "matched")
        self.assertIsNone(detector._classify_process(
            detector.ProcessObservation(42, 1, "droid", ["droid --prompt factory"])))

    def test_new_installed_clis_are_discovered_without_recursing(self):
        expected = {"kimi": "Kimi Code", "kilo": "Kilo Code", "vibe": "Mistral Vibe",
                    "crush": "Crush", "junie": "Junie CLI", "auggie": "Auggie", "devin": "Devin CLI"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for executable in expected:
                (root / executable).touch()
            (root / "nested").mkdir()
            (root / "nested" / "kimi-agent").touch()
            with patch.object(detector, "_executable_roots", return_value=(root,)), \
                 patch.object(detector, "_hash_roots", return_value=(root,)):
                assets = detector.scan_installed_clis()
            self.assertEqual({a.metadata["package"]: a.name for a in assets}, expected)
            self.assertNotIn(str(root), json.dumps([a.to_dict() for a in assets]))

    def test_docker_option_value_is_not_treated_as_agent_identity(self):
        unrelated = detector.ProcessObservation(
            10, 1, "/usr/local/bin/docker", ["docker run --name opencode ubuntu:latest"]
        )
        agent_image = detector.ProcessObservation(
            11, 1, "/usr/local/bin/docker", ["docker run --rm example/dify:latest"]
        )
        self.assertIsNone(detector._classify_process(unrelated))
        self.assertEqual(detector._classify_process(agent_image), ("Dify", "Dify"))

    def test_installed_cli_scan_is_bounded_and_hashes_paths(self):
        executable = Path("/private/example/.local/bin/codex")
        with patch.object(detector, "_installed_cli_candidates", return_value=iter([
            ("OpenAI Codex", "OpenAI", executable),
        ])):
            asset = detector.scan_installed_clis()[0]
        serialized = json.dumps(asset.to_dict())
        self.assertEqual(asset.kind, "application")
        self.assertEqual(asset.name, "OpenAI Codex")
        self.assertEqual(asset.metadata["package"], "codex")
        self.assertNotIn(str(executable.parent), serialized)

    def test_cli_candidate_search_does_not_recurse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            direct = root / "codex"
            nested = root / "nested" / "claude"
            direct.touch()
            nested.parent.mkdir()
            nested.touch()
            with patch.object(detector, "_executable_roots", return_value=(root,)):
                candidates = list(detector._installed_cli_candidates())
        self.assertEqual([(name, path.name) for name, _vendor, path in candidates], [
            ("OpenAI Codex", "codex"),
        ])

    def test_windows_app_scan_checks_standard_product_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            program_files = root / "Program Files"
            local_programs = root / "Local" / "Programs"
            (program_files / "Cursor").mkdir(parents=True)
            (local_programs / "Claude").mkdir(parents=True)
            (local_programs / "Unrelated").mkdir()
            environment = {
                "ProgramFiles": str(program_files),
                "LOCALAPPDATA": str(root / "Local"),
            }
            with patch.object(detector.platform, "system", return_value="Windows"), \
                 patch.dict(detector.os.environ, environment, clear=True):
                names = {asset.name for asset in detector.scan_installed_apps()}
        self.assertEqual(names, {"Claude", "Cursor"})

    def test_binary_hashing_stays_inside_safe_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "safe"
            outside = Path(directory).resolve() / "Documents"
            root.mkdir()
            outside.mkdir()
            binary = root / "codex"
            private = outside / "codex"
            binary.write_bytes(b"known executable")
            private.write_bytes(b"private file")
            with patch.object(detector, "_hash_roots", return_value=(root,)):
                binary_hash, status = detector._binary_evidence("OpenAI Codex", binary)
                self.assertIsNone(detector._safe_binary_path(private))
        self.assertEqual(len(binary_hash), 64)
        self.assertEqual(status, "unlisted")

    def test_process_detection_never_emits_command(self):
        with patch.object(detector, "_process_rows", return_value=[
            detector.ProcessObservation(12, 1, "/Applications/Ollama.app/ollama", ["serve", "--api-key=very-secret"])
        ]):
            asset = detector.scan_processes()[0]
        self.assertEqual(asset.name, "Ollama")
        self.assertTrue(asset.running)
        self.assertNotIn("very-secret", json.dumps(asset.to_dict()))
        self.assertEqual(len(asset.command_hash), 64)

    def test_sensitive_command_values_are_redacted_before_hashing(self):
        redacted = detector._safe_command_fingerprint(["run", "--token", "secret-one"])
        different_secret = detector._safe_command_fingerprint(["run", "--token", "secret-two"])
        self.assertEqual(redacted, different_secret)

    def test_single_ps_command_line_redacts_prompt_values(self):
        first = detector._safe_command_fingerprint(["codex --prompt 'private one'"])
        second = detector._safe_command_fingerprint(["codex --prompt 'private two'"])
        self.assertEqual(first, second)

    def test_positional_prompt_does_not_affect_command_fingerprint(self):
        first = detector._safe_command_fingerprint(["codex 'private one'"])
        second = detector._safe_command_fingerprint(["codex 'private two'"])
        node_entry = detector._safe_command_fingerprint(["node /opt/node_modules/@openai/codex/bin/codex.js one"])
        other_entry = detector._safe_command_fingerprint(["node /srv/unrelated.js one"])
        self.assertEqual(first, second)
        self.assertNotEqual(node_entry, other_entry)

    def test_classification_is_allowlisted(self):
        self.assertEqual(detector._classify("/usr/bin/ollama serve"), ("Ollama", "Ollama"))
        self.assertIsNone(detector._classify("/usr/bin/calculator"))

    def test_shell_command_text_does_not_create_false_positive(self):
        with patch.object(detector, "_process_rows", return_value=[
            detector.ProcessObservation(42, 1, "/bin/bash", ["echo install ollama"])
        ]):
            self.assertEqual(detector.scan_processes(), [])

    def test_agent_runtime_is_linked_to_host_app(self):
        rows = [
            detector.ProcessObservation(10, 1, "/Applications/Cursor.app/Contents/MacOS/Cursor", []),
            detector.ProcessObservation(11, 10, "/usr/local/bin/python3", ["python3", "-m", "langgraph", "serve"]),
            detector.ProcessObservation(12, 11, "/usr/local/bin/python3", ["python3", "-m", "langgraph", "serve"]),
        ]
        with patch.object(detector, "_process_rows", return_value=rows):
            assets = detector.scan_processes()
        runtime = next(asset for asset in assets if asset.kind == "agent_runtime")
        self.assertEqual(runtime.name, "LangGraph/LangChain")
        self.assertEqual(runtime.metadata["host_app"], "Cursor")
        self.assertEqual(runtime.metadata["instance_count"], 2)

    def test_macos_truncated_homebrew_python_comm_classifies_agent_runtimes(self):
        # macOS ``ps`` truncates long ``comm`` paths; the real Python.framework
        # binary remains as the leading token of ``args``.
        truncated_comm = "/opt/homebrew/Ce"
        cases = [
            ("crewai", ("CrewAI", "CrewAI")),
            ("autogen", ("AutoGen", "Microsoft")),
            ("langgraph", ("LangGraph/LangChain", "LangChain")),
            ("langchain", ("LangGraph/LangChain", "LangChain")),
            ("mcp-server", ("MCP Server", "Unknown")),
        ]
        for needle, expected in cases:
            with self.subTest(needle=needle):
                args_line = (
                    f"{HOMEBREW_FRAMEWORK_PYTHON} "
                    f"/Users/nick/.edgedisco-lab-debug/{needle}_workload.py"
                )
                row = detector.ProcessObservation(73269, 1, truncated_comm, [args_line])
                self.assertEqual(detector._classify_process(row), expected)
                with patch.object(detector, "_process_rows", return_value=[row]):
                    assets = detector.scan_processes()
                process_assets = [a for a in assets if a.kind == "process"]
                self.assertEqual(len(process_assets), 1)
                self.assertEqual(process_assets[0].name, expected[0])
                serialized = json.dumps(process_assets[0].to_dict())
                self.assertNotIn(args_line, serialized)
                self.assertNotIn(f"{needle}_workload.py", serialized)

    def test_short_product_names_do_not_match_inside_unrelated_names(self):
        self.assertIsNone(detector._classify("/usr/local/bin/raider"))
        self.assertIsNone(detector._classify("/Applications/Incline.app"))

    def test_unrelated_process_with_ai_keyword_in_args_is_not_classified(self):
        # Args are only consulted when the leading executable is a known generic
        # runtime; an unrelated binary must not match via arbitrary argv text.
        row = detector.ProcessObservation(
            99,
            1,
            "/usr/bin/grep",
            ["grep", "-n", "crewai", "/tmp/notes.txt"],
        )
        self.assertIsNone(detector._classify_process(row))
        with patch.object(detector, "_process_rows", return_value=[row]):
            self.assertEqual(detector.scan_processes(), [])


if __name__ == "__main__":
    unittest.main()
