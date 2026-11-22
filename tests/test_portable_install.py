import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import install


class PortableInstallationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / 'config').mkdir()
        self.env = patch.dict(os.environ, {})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_existing_vm_uses_legacy_forwards(self):
        vm = self.root / 'lima/wx'
        vm.mkdir(parents=True)
        (vm / 'lima.yaml').touch()
        self.assertEqual(33021, install.port_base(self.root, None))
        with self.assertRaises(ValueError):
            install.port_base(self.root, 34121)

    def test_custom_port_is_remembered_and_range_checked(self):
        self.assertEqual(34121, install.port_base(self.root, 34121))
        for port in (1023, 65534):
            with self.assertRaises(ValueError):
                install.port_base(self.root, port)
        (self.root/'config/port-base').write_text('34121\n')
        self.assertEqual(34121, install.port_base(self.root, None))
        self.assertEqual(33021, install.port_base(self.root, 33021))
        vm = self.root / 'lima/wx'
        vm.mkdir(parents=True)
        (vm / 'lima.yaml').touch()
        with self.assertRaises(ValueError):
            install.port_base(self.root, 33021)

    def test_empty_install_and_existing_author_file(self):
        with patch.object(install, 'APP', self.root, create=True):
            install.install_authors()
            persisted = self.root/'state/monitoring/authors.txt'
            self.assertEqual('', persisted.read_text())
            persisted.write_text('保存的账号\n')
            install.install_authors()
            self.assertEqual('保存的账号\n', (self.root/'config/authors.txt').read_text())
            self.assertTrue((self.root/'config/authors.txt').is_symlink())

    def test_existing_container_must_own_expected_profile_and_media(self):
        container = dict(Config={'Labels':{'com.docker.compose.project':'wechat-monitor',
                                           'com.docker.compose.service':'weixin'}},
                         Mounts=[dict(Destination='/config',Name='wechat-monitor_weixin-profile'),
                                 dict(Destination='/config/Downloads',Source=str(self.root/'media'))],
                         State={'Status':'running'},
                         HostConfig={'PortBindings':{f'{guest}/tcp':[dict(HostIp='127.0.0.1', HostPort=port)]
                                                     for guest, port in [(3000,'33021'),(3001,'33022')]}})
        def command(*args):
            return 'wx-playback' if 'ls' in args else json.dumps([container])

        with patch.object(install, 'APP', self.root, create=True), patch.object(install, 'DOCKER', [], create=True), \
                patch.object(install, 'output', side_effect=command):
            self.assertEqual('running', install.existing_player())
            container['Mounts'][0]['Name'] = 'unrelated-profile'
            with self.assertRaises(RuntimeError):
                install.existing_player()
