import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_asset_inventory import detector


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


if __name__ == "__main__":
    unittest.main()
