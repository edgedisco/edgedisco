import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, Mock

from ai_asset_inventory import detector, fingerprint_library as hashes
from ai_asset_inventory.path_policy import allowed_path
from ai_asset_inventory.self_service import detect_adapters


class ScanBoundaryTests(unittest.TestCase):
    def test_symlinked_root_and_file_are_rejected_before_target_stat(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            private = base / 'Documents'
            private.mkdir()
            (private / 'codex').write_bytes(b'private')
            linked_root = base / 'bin'
            linked_root.symlink_to(private, target_is_directory=True)
            original = Path.lstat
            def guarded(path):
                self.assertFalse(path == private or private in path.parents)
                return original(path)
            with patch.object(Path, 'lstat', guarded):
                self.assertIsNone(allowed_path(linked_root / 'codex', (linked_root,)))
            linked_root.unlink()
            linked_root.mkdir()
            (linked_root / 'codex').symlink_to(private / 'codex')
            with patch.object(Path, 'lstat', guarded):
                self.assertIsNone(allowed_path(linked_root / 'codex', (linked_root,)))

    def test_symlink_between_independently_allowed_roots_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            bin_dir, cellar = base / 'bin', base / 'cellar'
            bin_dir.mkdir(); cellar.mkdir()
            binary = cellar / 'codex'
            binary.write_bytes(b'agent')
            link = bin_dir / 'codex'
            link.symlink_to(binary)
            self.assertEqual(allowed_path(link, (bin_dir, cellar)), binary)

    def test_app_plist_and_mcp_links_do_not_open_protected_target(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            apps, private = base / 'Applications', base / 'Documents'
            apps.mkdir(); private.mkdir()
            (apps / 'Claude.app').symlink_to(private, target_is_directory=True)
            with patch.object(detector, '_application_roots', return_value=(apps,)), \
                 patch.object(Path, 'open', side_effect=AssertionError('must not read target')):
                self.assertIsNone(detector._mac_bundle_version(apps / 'Claude.app'))
                self.assertIsNone(detector._mac_bundle_executable(apps / 'Claude.app'))
            config = base / '.cursor'
            config.mkdir()
            (config / 'mcp.json').symlink_to(private / 'private.json')
            with patch.object(detector, '_mcp_candidates', return_value=(('Cursor', config / 'mcp.json'),)):
                self.assertEqual(list(detector._mcp_paths()), [])

    def test_setup_never_searches_path(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch('ai_asset_inventory.self_service.shutil.which', side_effect=AssertionError('PATH search')), \
             patch('ai_asset_inventory.self_service._installed_cli_candidates', return_value=iter([])):
            detect_adapters(Path(temp).resolve())

    def test_failed_or_malformed_process_query_is_not_empty_inventory(self):
        for result in (PermissionError(), Mock(stdout='broken process row'), Mock(stdout='')):
            options = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with self.subTest(result=result), patch.object(detector.subprocess, 'run', **options):
                with self.assertRaises(detector.ProcessScanUnavailable):
                    detector.scan_processes()

    def test_interpreter_parser_stops_before_application_arguments(self):
        cases = [
            ('python3 /srv/unrelated.py -m crewai', None),
            ('python3 /srv/crewai.py -c user-option', 'CrewAI'),
            ('node --require /tmp/crewai.js /srv/unrelated.js', None),
            ('node --require /tmp/util.js /srv/crewai.js --eval value', 'CrewAI'),
            ('npx --cache /tmp/crewai unrelated', None),
            ('python3 -W ignore -m crewai user-argument', 'CrewAI'),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                row = detector.ProcessObservation(1, 0, '/usr/bin/' + command.split()[0], [command])
                result = detector._classify_process(row)
                self.assertEqual(result[0] if result else None, expected)

    def test_growth_is_bounded_during_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'agent'
            path.write_bytes(b'x')
            before = path.stat()
            class Growing(io.BytesIO):
                def fileno(self): return 42
            stream = Growing(b'x' * 4096)
            @contextmanager
            def opened(_path): yield stream
            with patch.object(hashes, '_open_regular', opened), patch.object(hashes, 'os_fstat', return_value=before):
                self.assertIsNone(hashes.sha256_file(path, max_bytes=1))
                self.assertEqual(stream.tell(), 2)

    @unittest.skipUnless(os.name == 'posix', 'POSIX FIFO')
    def test_fifo_replacement_does_not_block_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'agent'
            path.write_bytes(b'x')
            before = path.stat()
            path.unlink()
            os.mkfifo(path)
            with patch.object(Path, 'lstat', return_value=before):
                self.assertIsNone(hashes.sha256_file(path))
