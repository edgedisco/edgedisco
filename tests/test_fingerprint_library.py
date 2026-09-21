import hashlib
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from ai_asset_inventory import fingerprint_library


class FingerprintLibraryTests(unittest.TestCase):
    def setUp(self):
        fingerprint_library._FILE_HASH_CACHE.clear()

    def test_static_library_is_valid_and_unique(self):
        entries = fingerprint_library.AGENT_FINGERPRINTS
        self.assertGreaterEqual(len(entries), 14)
        self.assertEqual(len({entry.name for entry in entries}), len(entries))
        self.assertTrue(all(entry.executables and entry.sources for entry in entries))
        self.assertRegex(fingerprint_library.LIBRARY_VERSION, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(files("ai_asset_inventory").joinpath("fingerprints.json").is_file())

    def test_hermes_agent_and_openclaw_are_catalogued(self):
        entries = {entry.name: entry for entry in fingerprint_library.AGENT_FINGERPRINTS}
        self.assertEqual(entries["Hermes Agent"].vendor, "Nous Research")
        self.assertIn("hermes", entries["Hermes Agent"].executables)
        self.assertEqual(entries["OpenClaw"].vendor, "OpenClaw")
        self.assertIn("openclaw", entries["OpenClaw"].executables)

    def test_file_sha256_matches_bytes_and_changes_after_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "agent"
            path.write_bytes(b"first")
            first = fingerprint_library.sha256_file(path)
            self.assertEqual(first, hashlib.sha256(b"first").hexdigest())
            path.write_bytes(b"second")
            second = fingerprint_library.sha256_file(path)
            self.assertEqual(second, hashlib.sha256(b"second").hexdigest())
            self.assertNotEqual(first, second)

    def test_non_regular_and_oversized_files_are_not_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            large = root / "large"
            large.write_bytes(b"12")
            self.assertIsNone(fingerprint_library.sha256_file(folder))
            self.assertIsNone(fingerprint_library.sha256_file(large, max_bytes=1))


if __name__ == "__main__":
    unittest.main()
