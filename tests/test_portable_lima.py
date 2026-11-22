import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'bootstrap_runtime', Path(__file__).resolve().parents[1] / 'deploy/bootstrap-runtime.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class PortableLimaTests(unittest.TestCase):
    def test_guest_download_survives_a_separate_invocation(self):
        content = b'image'
        source = ('guest.raw.gz', ('https://github.com/example/release',), len(content),
                  hashlib.sha256(content).hexdigest())
        with TemporaryDirectory() as folder:
            root = Path(folder)
            def interrupted(command):
                pending = Path(command[command.index('-o') + 1])
                if not pending.exists():
                    pending.write_bytes(content[:2])
                return SimpleNamespace(returncode=18)

            with patch.object(bootstrap.subprocess, 'run', side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    bootstrap.artifact(root, source)
            self.assertFalse((root / source[0]).exists())

            def resumed(command):
                self.assertIn('--continue-at', command)
                self.assertNotIn('Authorization: Bearer QQ==', command)
                pending = Path(command[command.index('-o') + 1])
                self.assertEqual(pending.read_bytes(), content[:2])
                with pending.open('ab') as out:
                    out.write(content[2:])
                return SimpleNamespace(returncode=0)

            with patch.object(bootstrap.subprocess, 'run', side_effect=resumed) as curl:
                self.assertEqual(bootstrap.artifact(root, source).read_bytes(), content)
                self.assertEqual(bootstrap.artifact(root, source).read_bytes(), content)
                curl.assert_called_once()
            self.assertEqual([path.name for path in root.iterdir()], [source[0]])

    def test_bottle_uses_https_and_drops_credentials_on_cross_host_redirects(self):
        with TemporaryDirectory() as folder:
            def inspect(command):
                self.assertEqual(command[1], '--disable')
                self.assertEqual(command[command.index('--proto-redir') + 1], '=https')
                self.assertIn('Authorization: Bearer QQ==', command)
                self.assertNotIn('--location-trusted', command)
                return SimpleNamespace(returncode=22)
            with patch.object(bootstrap.subprocess, 'run', side_effect=inspect) as curl:
                with self.assertRaises(RuntimeError):
                    bootstrap.artifact(Path(folder), bootstrap.LIMA_SEQUOIA)
                curl.assert_called_once()

    def test_newer_macos_can_fall_back_to_the_verified_sequoia_bottle(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            runtime = root / 'runtime'
            runtime.mkdir()
            bottle = root / 'lima.tgz'
            binary = b'portable'
            with tarfile.open(bottle, 'w:gz') as tar:
                item = tarfile.TarInfo('lima/2.3.0/bin/limactl')
                item.size = len(binary)
                item.mode = 0o755
                tar.addfile(item, io.BytesIO(binary))
            with patch.object(bootstrap.platform, 'mac_ver', return_value=('26.2', (), 'arm64')), \
                 patch.object(bootstrap, 'SEQUOIA_LIMACTL_SHA', hashlib.sha256(binary).hexdigest()), \
                 patch.object(bootstrap, 'artifact', side_effect=[bootstrap.DownloadError('TLS'), bottle]) as fetch:
                bootstrap.install_lima(runtime, root)
                self.assertEqual([args.args[1] for args in fetch.call_args_list],
                                 [bootstrap.LIMA_TAR, bootstrap.LIMA_SEQUOIA])
                self.assertEqual((runtime / 'bin/limactl').resolve(), runtime / 'lima-2.3.0-sequoia/bin/limactl')
                self.assertFalse((runtime / 'lima-2.3.0').exists())
                (runtime / 'bin/limactl').unlink()
                bootstrap.install_lima(runtime, root)
                self.assertEqual(fetch.call_count, 2)

    def test_sequoia_install_survives_macos_upgrade_and_rejects_changed_binary(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            runtime = root / 'runtime'
            runtime.mkdir()
            archive = root / 'bottle.tgz'
            binary = b'portable limactl'
            with tarfile.open(archive, 'w:gz') as tar:
                for name, data in [('bin/limactl', binary), ('LICENSE', b'license')]:
                    item = tarfile.TarInfo('lima/2.3.0/' + name)
                    item.mode = 0o755 if name.endswith('limactl') else 0o644
                    item.size = len(data)
                    tar.addfile(item, io.BytesIO(data))
            with patch.object(bootstrap, 'SEQUOIA_LIMACTL_SHA', hashlib.sha256(binary).hexdigest()), \
                 patch.object(bootstrap, 'artifact', return_value=archive) as fetch, \
                 patch.object(bootstrap.platform, 'mac_ver') as version:
                version.return_value = ('15.7', (), 'arm64')
                bootstrap.install_lima(runtime, root)
                fetch.assert_called_once_with(root, bootstrap.LIMA_SEQUOIA)
                self.assertEqual((runtime / 'lima-2.3.0-sequoia/LICENSE').read_bytes(), b'license')
                version.return_value = ('26.2', (), 'arm64')
                bootstrap.install_lima(runtime, root)
                fetch.assert_called_once()
                (runtime / 'bin/limactl').write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    bootstrap.install_lima(runtime, root)
