import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'bootstrap_runtime', Path(__file__).resolve().parents[1] / 'deploy/bootstrap-runtime.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class PortableLimaTests(unittest.TestCase):
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
