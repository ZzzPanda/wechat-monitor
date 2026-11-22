"""The portable installer must not use partial or invalid downloads as executables."""
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import host_tools


class HostToolDownloadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cache = Path(folder.name)
        self.spec = dict(name='tool', size=4, sha256=hashlib.sha256(b'good').hexdigest(),
                         url='https://primary.example/tool', fallback='https://secondary.example/tool')
        lock = patch.dict(host_tools.LOCK, {'sample': self.spec})
        lock.start()
        self.addCleanup(lock.stop)

    def test_first_bad_download_falls_back_then_reuses_verified_cache(self):
        def download(command):
            Path(command[command.index('-o') + 1]).write_bytes(b'good' if command[-1] == self.spec['fallback'] else b'bad!')
            return SimpleNamespace(returncode=0)
        with patch.object(host_tools.subprocess, 'run', side_effect=download) as curl:
            self.assertEqual(host_tools.fetch('sample', self.cache).read_bytes(), b'good')
            self.assertEqual(host_tools.fetch('sample', self.cache).read_bytes(), b'good')
            self.assertEqual(curl.call_count, 2)

    def test_partial_transfer_resumes_and_verifies_whole_file(self):
        def download(command):
            file = Path(command[command.index('-o') + 1])
            if not file.exists():
                file.write_bytes(b'go')
                return SimpleNamespace(returncode=18)
            self.assertIn('--continue-at', command)
            file.write_bytes(file.read_bytes() + b'od')
            return SimpleNamespace(returncode=0)
        with patch.object(host_tools.subprocess, 'run', side_effect=download) as curl:
            self.assertEqual(host_tools.fetch('sample', self.cache).read_bytes(), b'good')
            self.assertEqual(curl.call_count, 2)

    def test_corrupt_existing_cache_is_rejected_after_transfer_failure(self):
        target = self.cache / 'tool'
        target.write_bytes(b'bad!')
        with patch.object(host_tools.subprocess, 'run', return_value=SimpleNamespace(returncode=22)):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                host_tools.fetch('sample', self.cache)
        self.assertFalse(host_tools.valid(target, self.spec))
        self.assertEqual(list(self.cache.iterdir()), [target])
