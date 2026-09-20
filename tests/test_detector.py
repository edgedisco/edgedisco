import json
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
                    f"/Users/nick/.edgedisco-lab-debug/workload.py {needle}"
                )
                row = detector.ProcessObservation(73269, 1, truncated_comm, [args_line])
                text = detector._process_classification_text(row)
                self.assertIn(needle, text.lower())
                self.assertEqual(detector._classify(text), expected)
                with patch.object(detector, "_process_rows", return_value=[row]):
                    assets = detector.scan_processes()
                process_assets = [a for a in assets if a.kind == "process"]
                self.assertEqual(len(process_assets), 1)
                self.assertEqual(process_assets[0].name, expected[0])
                serialized = json.dumps(process_assets[0].to_dict())
                self.assertNotIn(args_line, serialized)
                self.assertNotIn("workload.py", serialized)

    def test_unrelated_process_with_ai_keyword_in_args_is_not_classified(self):
        # Args are only consulted when the leading executable is a known generic
        # runtime; an unrelated binary must not match via arbitrary argv text.
        row = detector.ProcessObservation(
            99,
            1,
            "/usr/bin/grep",
            ["grep", "-n", "crewai", "/tmp/notes.txt"],
        )
        self.assertEqual(detector._process_classification_text(row), "/usr/bin/grep")
        with patch.object(detector, "_process_rows", return_value=[row]):
            self.assertEqual(detector.scan_processes(), [])


if __name__ == "__main__":
    unittest.main()
