from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wechat_paths as paths


class PortablePathsTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(dir='/tmp', prefix='wp-')
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.root = self.base / '自选目录 with spaces'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        home = self.base / 'home'
        home.mkdir()
        self.stack.enter_context(patch.object(Path, 'home', return_value=home))
        self.stack.enter_context(patch.dict(os.environ, {'WECHAT_CONFIG_DIR': str(self.base / 'config'),
                                                        'WECHAT_DATA_ROOT': ''}))
        self.stack.enter_context(patch.object(paths, 'LEGACY_ROOT', self.base / 'legacy'))
        self.device = self.stack.enter_context(patch.object(paths, 'volume_uuid', return_value=('uuid-A', self.base)))

    def test_switch_default_only_after_success_and_reuse_selection(self):
        old = self.base / 'old'
        paths.check_drive(old, initialize=True)
        paths.save_selection(old)
        paths.check_drive(self.root, initialize=True)
        self.assertEqual(paths.resolve_root(), old)
        paths.save_selection(self.root)
        self.assertEqual(paths.resolve_root(), self.root)
        self.assertEqual(paths.resolve_root(old), old)
        self.assertEqual(paths.saved_location(), (self.root, 'uuid-A'))
        paths.check_drive(self.root)

    def test_missing_marker_or_other_volume_cannot_reinitialize_saved_path(self):
        paths.check_drive(self.root, initialize=True)
        paths.save_selection(self.root)
        self.device.return_value = 'uuid-B', self.base
        with self.assertRaises(RuntimeError):
            paths.check_drive(self.root, initialize=True)
        self.device.return_value = 'uuid-A', self.base
        (self.root / paths.MARKER).unlink()
        self.root.rmdir()
        with self.assertRaises(RuntimeError):
            paths.check_drive(self.root, initialize=True)
        self.assertFalse(self.root.exists())

    def test_known_legacy_deployment_is_adopted_only_on_its_original_volume(self):
        root = paths.LEGACY_ROOT
        (root / 'lima/wx').mkdir(parents=True)
        (root / 'lima/wx/lima.yaml').touch()
        self.assertEqual(paths.resolve_root(), root)
        with self.assertRaises(RuntimeError):
            paths.check_drive(root, initialize=True)
        self.device.return_value = paths.LEGACY_VOLUME_UUID, self.base
        paths.check_drive(root, initialize=True)
        self.assertEqual(paths.lima_home(root), root / 'lima')

    def test_short_lima_path_fallback_is_stable_and_separate_per_installation(self):
        self.assertNotEqual(paths.lima_home(self.root), self.root / 'lima')
        self.assertNotEqual(paths.lima_home(self.root), paths.lima_home(self.root / 'other'))
        self.assertEqual(paths.lima_home(Path('/Volumes/data/wechat')), Path('/Volumes/data/wechat/lima'))

    def test_unsafe_docker_url_uses_short_socket_and_registered_instance_wins(self):
        for suffix in ('100%real', 'team#one', 'first?next'):
            with self.subTest(suffix=suffix):
                root = Path('/Volumes/data') / suffix
                self.assertNotEqual(paths.lima_home(root), root / 'lima')
        paths.check_drive(self.root, initialize=True)
        chosen = paths.lima_home(self.root)
        paths.prepare_vm_disk(self.root)
        (self.root / 'lima/wx').mkdir(parents=True)
        self.assertEqual(paths.lima_home(self.root), chosen)
        (self.root / 'vm/disk').write_bytes(b'persisted guest')
        with self.assertRaisesRegex(RuntimeError, '配置丢失'):
            paths.prepare_vm_disk(self.root)
        (self.root / 'vm/.instance').write_text('/unexpected/instance')
        with self.assertRaisesRegex(RuntimeError, '不同'):
            paths.lima_home(self.root)

    def test_colon_in_data_path_gives_actionable_error_before_initialization(self):
        root = self.base / 'work:private'
        with self.assertRaisesRegex(ValueError, '冒号'):
            paths.check_drive(root, initialize=True)
        self.assertFalse(root.exists())

    def test_unmarked_source_or_existing_unrelated_directory_cannot_be_overwritten(self):
        self.root.mkdir()
        (self.root / 'scripts').mkdir()
        with self.assertRaisesRegex(ValueError, '其他文件'):
            paths.check_drive(self.root, initialize=True)
        self.assertFalse((self.root / paths.MARKER).exists())
        (self.root / 'scripts').rmdir()
        (self.root / 'images').mkdir()
        paths.check_drive(self.root, initialize=True)
        self.assertTrue((self.root / paths.MARKER).exists())

    def test_guest_disk_follows_data_root_and_survives_retry(self):
        paths.check_drive(self.root, initialize=True)
        paths.prepare_vm_disk(self.root)
        disk = paths.lima_home(self.root) / 'wx/disk'
        disk.parent.mkdir(parents=True)
        disk.write_bytes(b'guest with a local login')
        paths.place_vm_disk(self.root)
        target = self.root / 'vm/disk'
        self.assertEqual(disk.readlink(), target)
        paths.place_vm_disk(self.root)
        self.assertEqual(target.read_bytes(), b'guest with a local login')
        target.unlink()
        with self.assertRaisesRegex(RuntimeError, '磁盘丢失'):
            paths.place_vm_disk(self.root)

    def test_mismatching_or_symlink_disk_does_not_overwrite_existing_files(self):
        paths.check_drive(self.root, initialize=True)
        paths.prepare_vm_disk(self.root)
        disk = paths.lima_home(self.root) / 'wx/disk'
        disk.parent.mkdir(parents=True)
        disk.write_bytes(b'original')
        target = self.root / 'vm/disk'
        target.write_bytes(b'unrelated')
        with self.assertRaisesRegex(RuntimeError, '不同的虚拟机磁盘'):
            paths.place_vm_disk(self.root)
        self.assertEqual(disk.read_bytes(), b'original')
        self.assertEqual(target.read_bytes(), b'unrelated')
        target.unlink()
        target.symlink_to(disk)
        with self.assertRaises(RuntimeError):
            paths.place_vm_disk(self.root)

    def test_escaped_child_or_missing_external_mount_rejected(self):
        self.root.mkdir()
        (self.root / 'cache').symlink_to(self.base)
        with self.assertRaises(ValueError):
            paths.child_on_drive(self.root, self.root / 'cache/inside')
        with self.assertRaises(RuntimeError):
            paths.check_drive(Path('/Volumes/not-mounted-for-portable-test/abc'), initialize=True)


if __name__ == '__main__':
    unittest.main()
