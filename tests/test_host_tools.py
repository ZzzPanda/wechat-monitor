"""The portable installer must not use partial or invalid downloads as executables."""
import hashlib
import io
import json
import os
import platform
import shlex
import subprocess
import tarfile
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


@unittest.skipUnless(sys.platform == 'darwin' and platform.machine() == 'arm64' and
                     int(platform.mac_ver()[0].split('.')[0]) >= 15, 'supported macOS Apple Silicon bootstrap')
class ShellBootstrapTests(unittest.TestCase):
    def test_bootstrap_retries_tls_resumes_transfer_and_preserves_cli_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            repo = work / 'source repo'
            deploy = repo / 'deploy'
            deploy.mkdir(parents=True)
            bundle = work / 'pinned.tgz'
            launcher = ('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n').encode()
            with tarfile.open(bundle, 'w:gz') as archive:
                entry = tarfile.TarInfo('python/bin/python3.12')
                entry.size = len(launcher)
                entry.mode = 0o755
                archive.addfile(entry, io.BytesIO(launcher))
            data = bundle.read_bytes()
            spec = dict(name=bundle.name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        url='https://primary.example/pinned', fallback='https://secondary.example/pinned')
            (deploy / 'host-tools.lock.json').write_text(json.dumps({'python': spec}))
            fake = work / 'fake-curl'
            response = work / 'download.py'
            response.write_text(
                'from pathlib import Path\nimport sys\n'
                'assert sys.argv[1] == "--disable"\n'
                'assert sys.argv[-1] == "https://primary.example/pinned"\n'
                'attempt = Path(__file__).with_name("tls-retry")\n'
                'if not attempt.exists():\n    attempt.touch()\n    sys.exit(35)\n'
                'file = Path(sys.argv[sys.argv.index("-o")+1])\n'
                'data = Path(__file__).with_name("pinned.tgz").read_bytes()\n'
                'if not file.exists():\n'
                '    file.write_bytes(data[:10])\n    sys.exit(18)\n'
                'assert "--continue-at" in sys.argv and file.read_bytes() == data[:10]\n'
                'file.write_bytes(file.read_bytes() + data[10:])\n')
            fake.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                            shlex.quote(str(response)) + ' \"$@\"\n')
            fake.chmod(0o755)
            source = host_tools.HERE.parent / 'deploy.sh'
            start = repo / 'deploy.sh'
            start.write_text(source.read_text().replace('/usr/bin/curl', shlex.quote(str(fake))))
            start.chmod(0o755)
            (deploy / 'install.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            args = ['--data-dir', str(work / '新的 目录'), '--port-base', '34721']
            env = dict(HOME=str(work / 'home'), PATH=os.defpath)
            first = subprocess.check_output([start, *args], env=env, text=True)
            self.assertEqual(json.loads(first), args)
            fake.unlink()
            self.assertEqual(subprocess.check_output([start, *args], env=env, text=True), first)
