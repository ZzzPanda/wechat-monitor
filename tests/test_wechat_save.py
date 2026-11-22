"""Exercise replay after interrupted publication without touching the production index."""
import fcntl
import io
import json
from contextlib import ExitStack, redirect_stdout
import os
from pathlib import Path
import runpy
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
entry = runpy.run_path(str(ROOT / 'scripts' / 'wechat-save'))
save = entry['save']
preview = entry['preview']
recordings = entry['recordings']


class Saving(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='save-regression-')
        self.addCleanup(temporary.cleanup)
        instance = Path(temporary.name).resolve()
        (instance / '.wechat-monitor-volume').write_text('test-volume\n')
        self.root = instance / 'tmp'
        self.root.mkdir()
        self.media = self.root / 'media'
        self.media.mkdir()
        self.settings_path = self.root / 'settings.json'
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        preview.prepare(self.db)
        self.urls = ['https://weixin.qq.com/sph/test1', 'https://weixin.qq.com/sph/test2']
        fields = dict(author='测试作者', published='2026-11-01', caption='同一条作品', cover_sha256='1'*64)
        for url in self.urls:
            preview.record(self.db, url, fields)
        self.item = dict(self.db.execute('SELECT * FROM works').fetchone())
        self.fixture = self.root / 'fixture.mp4'
        subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-f','lavfi','-i','testsrc2=s=64x64:r=12',
                        '-f','lavfi','-i','sine','-t','1.5','-c:v','libx264','-c:a','aac',str(self.fixture)],check=True)
        self.starts = 0
        def take(db, item, folder):
            self.starts += 1
            shutil.copyfile(self.fixture, folder / 'raw.mp4')
            return dict(start=0.0, end=1.0, duration=1.0, last_position=0), lambda: True
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('wechat_paths.volume_uuid', return_value=('test-volume', instance.parent)))
        stack.enter_context(patch('wechat_paths.DATA_ROOT', instance))
        stack.enter_context(patch('wechat_clip.BASE', instance))
        stack.enter_context(patch('capture_wechat_player.BASE', instance))
        stack.enter_context(patch.dict(save.__globals__, ROOT=instance, STAGING=self.root / 'staging',
                                      STATE=self.root, MONITORING=self.root / 'monitoring', take_from_player=take))
        stack.enter_context(patch.multiple(recordings, MEDIA=self.media / '微信视频号',
                                           STORAGE_ROOT=self.media, SETTINGS_FILE=self.settings_path))

    def select_storage(self, name):
        folder = self.media / name
        self.settings_path.write_text(json.dumps({'storage_path': str(folder)}), encoding='utf-8')
        return folder

    def test_resume_after_publish_and_after_repair(self):
        # Both a new file and a previously missing registered file can crash after link(), before SQL commit.
        first_url, alias = self.urls
        register = recordings.register
        with patch.object(recordings, 'register', side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                save(self.db, first_url, self.item)
        journal = self.root / 'staging' / self.item['fingerprint'] / 'job.json'
        old_state = json.loads(journal.read_text())
        del old_state['destination']
        journal.write_text(json.dumps(old_state))
        selected = self.select_storage('new location')
        self.assertEqual(save(self.db, alias, self.item)['status'], 'saved')
        self.assertEqual(save(self.db, first_url, self.item)['status'], 'already_saved')
        self.assertEqual(self.starts, 1)
        record = recordings.lookup(self.db, first_url)
        destination = recordings.media_path(record['path'])
        self.assertTrue(destination.is_relative_to(recordings.MEDIA))
        self.assertFalse(selected.exists())
        destination.unlink()
        self.db.execute("UPDATE recordings SET sha256=?", ('f'*64,))
        self.db.commit()
        with patch.object(recordings, 'register', side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                save(self.db, alias, self.item)
        self.assertEqual(recordings.lookup(self.db, alias)['status'], 'changed')
        self.assertEqual(save(self.db, first_url, self.item)['status'], 'saved')
        self.assertEqual(self.starts, 2)
        self.assertEqual(recordings.lookup(self.db, alias)['status'], 'verified')
        self.assertIs(recordings.register, register)
        destination.write_bytes(b'changed after registration')
        with self.assertRaises(entry['SaveError']):
            save(self.db, first_url, self.item)
        self.assertEqual(destination.read_bytes(), b'changed after registration')

    def test_custom_storage_remains_pinned_through_retry_and_repair(self):
        initial = self.select_storage('微信视频号/项目归档/视频')
        first_url, alias = self.urls
        with patch.object(recordings, 'register', side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                save(self.db, first_url, self.item)
        [published] = initial.rglob('*.mp4')
        selected = self.select_storage('new location')
        self.assertEqual(save(self.db, alias, self.item)['status'], 'saved')
        registered = recordings.lookup(self.db, first_url)
        self.assertEqual(registered['status'], 'verified')
        self.assertEqual(registered['path'], './' + published.relative_to(self.media).as_posix())
        self.assertEqual(recordings.media_path(registered['path']), published)
        self.assertFalse(selected.exists())
        self.assertEqual(self.starts, 1)
        self.assertEqual(recordings.register(self.db, alias, published, replace=False)['status'], 'already_registered')
        other = 'https://weixin.qq.com/sph/different'
        preview.record(self.db, other, dict(author='测试作者', published='2026-11-02',
                                            caption='另一条作品', cover_sha256='2' * 64))
        with self.assertRaisesRegex(ValueError, 'already assigned'):
            recordings.register(self.db, other, published, replace=False)

        create_poster = runpy.run_path(str(ROOT / 'deploy' / 'posters.py'))['create_poster']
        cache = self.root / 'posters'
        cache.mkdir()
        key = self.item['fingerprint']
        self.assertTrue(create_poster((key, registered['path'], registered['sha256'], registered['size']), self.media, cache))
        self.assertTrue((cache / (key + '.jpg')).is_file())

        published.unlink()
        self.assertEqual(save(self.db, alias, self.item)['status'], 'saved')
        self.assertTrue(published.is_file())
        self.assertFalse(selected.exists())
        self.assertEqual(self.starts, 2)

    def test_captured_checkpoint_uses_original_url_across_retry(self):
        import wechat_clip
        original = wechat_clip.clip_recording
        def stop_after_file(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('simulated crash before checkpoint')
        with patch.object(wechat_clip, 'clip_recording', side_effect=stop_after_file):
            with self.assertRaisesRegex(RuntimeError, 'before checkpoint'):
                save(self.db, self.urls[0], self.item)
        self.assertEqual(save(self.db, self.urls[1], self.item)['status'], 'saved')
        self.assertEqual(self.starts, 1)

    def test_interrupted_take_is_retried_from_start(self):
        def interrupted(db, item, folder):
            (folder / 'raw.mp4').write_bytes(b'partial')
            raise KeyboardInterrupt
        with patch.dict(save.__globals__, take_from_player=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                save(self.db, self.urls[0], self.item)
        self.assertEqual(recordings.lookup(self.db, self.urls[1])['status'], 'not_registered')
        self.assertEqual(save(self.db, self.urls[1], self.item)['status'], 'saved')
        self.assertEqual(self.starts, 1)

    def test_completed_mkv_survives_remux_failure_and_retries_without_playing(self):
        import capture_wechat_player as recording
        from wechat_player import Player
        mkv = self.root / 'source.mkv'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', self.fixture, '-c:v', 'copy',
                        '-c:a', 'aac', '-ar', '48000', mkv], check=True)
        starts = []
        clock_origin = time.monotonic() - 5
        class RecordedFixture(recording.LiveCapture):
            def start(self):
                starts.append(self)
                self._stage = tempfile.TemporaryDirectory(prefix='wechat-player-', dir=self.output.parent)
                self._raw = Path(self._stage.name) / 'capture.mkv'
                shutil.copyfile(mkv, self._raw)
                self._proc = MagicMock()
                self._proc.poll.return_value = self._proc.wait.return_value = 0
                self._reader = Mock()
                self._finished.set()
                self._early, self.origin, self.video_wall_origin = True, clock_origin, 1000.0
                return self.origin
        player = Mock(spec=Player)
        player.prepare.return_value = dict(duration=1, window=42, crop=(0, 0, 64, 64))
        player.copy_share_url.return_value = self.urls[0]
        player.restart.return_value = clock_origin+.15
        player.watch.return_value = dict(end_monotonic=clock_origin+1.15, first_change_monotonic=clock_origin+1.50,
                                          start_guest_wall=1000.15, end_guest_wall=1001.15,
                                          first_change_guest_wall=1001.50, last_position=1)
        player.reference = None
        with ExitStack() as stack:
            stack.enter_context(patch.dict(save.__globals__, take_from_player=entry['take_from_player'],
                                           check_live_tools=lambda: None, work=lambda *a, **kw: self.item))
            stack.enter_context(patch('wechat_player.Player', return_value=player))
            stack.enter_context(patch.object(recording, 'LiveCapture', RecordedFixture))
            stack.enter_context(patch.object(recording, 'assert_quiet_before_playback'))
            with patch.object(RecordedFixture, '_validate', side_effect=RuntimeError('simulated remux failure')):
                with self.assertRaisesRegex(RuntimeError, 'simulated remux failure'):
                    save(self.db, self.urls[0], self.item)
            stage = self.root / 'staging' / self.item['fingerprint']
            jobpath, source = stage / 'job.json', stage / 'capture.mkv'
            old = jobpath.read_bytes()
            job = json.loads(old)
            self.assertEqual(job['phase'], 'recorded')
            self.assertEqual(job['evidence']['visible_url'], self.urls[0])
            self.assertEqual(recordings.lookup(self.db, self.urls[1])['status'], 'not_registered')
            self.assertFalse((stage / 'raw.mp4').exists())
            keep = stage / 'original'
            source.rename(keep)
            # Even a byte-identical copy cannot silently substitute for this take's inode.
            shutil.copy2(keep, source)
            with self.assertRaisesRegex(entry['SaveError'], '完整性'):
                save(self.db, self.urls[1], self.item)
            source.unlink()
            with self.assertRaisesRegex(entry['SaveError'], '缺失'):
                save(self.db, self.urls[1], self.item)
            keep.rename(source)
            stat = source.stat()
            with source.open('r+b') as f:
                first = f.read(1)
                f.seek(0)
                f.write(bytes([first[0] ^ 1]))
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            with self.assertRaisesRegex(entry['SaveError'], '完整性'):
                save(self.db, self.urls[1], self.item)
            with source.open('r+b') as f:
                f.write(first)
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            job['evidence']['duration'] = 3
            jobpath.write_text(json.dumps(job))
            with self.assertRaisesRegex(entry['SaveError'], '证据'):
                save(self.db, self.urls[1], self.item)
            jobpath.write_bytes(old)
            result = save(self.db, self.urls[1], self.item)
            self.assertEqual(result['status'], 'saved')
            self.assertEqual(len(starts), 1)
            player.watch.assert_called_once()
            self.assertFalse(stage.exists())

    def test_raw_checkpoint_only_after_clean_recorder_exit(self):
        from capture_wechat_player import LiveCapture
        for code, drained in ((1, True), (0, False)):
            with self.subTest(code=code, drained=drained):
                cap = LiveCapture(crop=(0, 0, 64, 64), duration=10, output=self.root / 'tmp-output.mp4')
                cap._proc, cap._reader, cap.origin = MagicMock(), Mock(), 10.0
                cap._proc.poll.return_value = cap._proc.wait.return_value = code
                if drained:
                    cap._finished.set()
                callback = Mock()
                with self.assertRaisesRegex(RuntimeError, 'Capture failed'):
                    cap.finish(on_recorded=callback)
                callback.assert_not_called()

    def test_short_source_is_not_checkpointed_even_with_valid_watcher_times(self):
        folder = self.root / 'incomplete'
        folder.mkdir()
        job = dict(version=1, key=self.item['fingerprint'], phase='recording', url=self.urls[0])
        entry['write_journal'](folder, job)
        evidence = dict(restart=100.2, origin=100.0, duration=5.0,
                        tail=dict(end_monotonic=105.2, first_change_monotonic=105.5, last_position=5))
        entry['check_playback_timing'](5, 5, evidence['tail'])
        with self.assertRaisesRegex(entry['SaveError'], '未覆盖'):
            entry['checkpoint_recording'](folder, self.item, evidence, self.fixture, True)
        self.assertEqual(entry['read_journal'](folder, self.item['fingerprint'])['phase'], 'recording')
        self.assertFalse((folder / 'capture.mkv').exists())

    def test_sigterm_stops_only_the_verified_short_work_before_cleanup(self):
        import numpy as np
        from PIL import Image
        from wechat_player import Player
        events = []
        player = Player('测试作者', '用于中断测试的短片')
        player.window, player.size, player.duration = 42, (1000, 800), 14
        player.identity_box = (100, 670, 10, 4)
        player.identity_regions = [(slice(0, 2), slice(None)), (slice(2, 4), slice(None))]
        picture = np.tile(np.array([0, 255] * 5, dtype='uint8'), (4, 1))
        player.reference = [picture[r] > 70 for r in player.identity_regions]
        player.controls = dict(seek=(200, 630), right=900)
        player._image = Mock(return_value=(0, Image.fromarray(picture)))
        player.prepare = Mock(return_value=dict(window=42, crop=(0, 0, 64, 64), duration=14))
        player.copy_share_url = Mock(return_value=self.urls[0])
        def restart():
            player.start = time.monotonic()
            return player.start
        player.restart = restart
        frames, recording = MagicMock(), MagicMock()
        frames.__enter__.return_value.read.side_effect = lambda **kw: os.kill(os.getpid(), signal.SIGTERM)
        frames.__exit__.side_effect = lambda *a: events.append('frames cleaned')
        recording.__enter__.return_value.start.return_value = time.monotonic() - 1
        recording.__exit__.side_effect = lambda *a: events.append('capture cleaned')
        output = io.StringIO()
        with ExitStack() as stack:
            old = signal.getsignal(signal.SIGTERM)
            self.addCleanup(signal.signal, signal.SIGTERM, old)
            stack.enter_context(patch.dict(save.__globals__, check_live_tools=lambda: None,
                                           work=lambda *a, **kw: self.item))
            stack.enter_context(patch('wechat_player.Player', return_value=player))
            key = stack.enter_context(patch('wechat_player.guest', side_effect=lambda *a: events.append('closed')))
            stack.enter_context(patch('wechat_frames.FrameStream', return_value=frames))
            stack.enter_context(patch('capture_wechat_player.LiveCapture', return_value=recording))
            stack.enter_context(patch('capture_wechat_player.assert_quiet_before_playback'))
            # Exercise the entry point's real SIGTERM handler and exit status using only test doubles.
            stack.enter_context(patch('argparse.ArgumentParser.parse_args', side_effect=lambda:
                                      entry['take_from_player'](self.db, self.item, self.root)))
            stack.enter_context(redirect_stdout(output))
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / 'scripts' / 'wechat-save'), run_name='__main__')
        self.assertEqual(stopped.exception.code, 130)
        self.assertEqual(json.loads(output.getvalue())['status'], 'interrupted')
        self.assertEqual(events, ['closed', 'frames cleaned', 'capture cleaned'])
        recording.__enter__.return_value.finish.assert_not_called()
        self.assertEqual(key.call_count, 1)
        self.assertIn('getactivewindow', key.call_args.args[2])
        self.assertEqual(key.call_args.args[-1], player.window)
        player._image.assert_called_once_with(player.identity_box)

    def test_interrupt_leaves_different_work_and_window_alone(self):
        import numpy as np
        from PIL import Image
        from wechat_player import Player, PlayerError
        for mode in ('automatically advanced', 'window changed'):
            with self.subTest(mode=mode):
                player = Mock(spec=Player)
                player.reference = [np.ones((2, 4), dtype=bool)] * 2
                player.identity_box = (0, 0, 4, 4)
                player.identity_regions = [(slice(0, 2), slice(None)), (slice(2, 4), slice(None))]
                # The account still matches, but the caption belongs to a different work.
                frame = Image.new('L', (4, 4), 255)
                frame.paste(0, (0, 0, 4, 2))
                player._image.return_value = (0, frame)
                if mode == 'window changed':
                    player._image.side_effect = PlayerError('lost focus')
                guard = entry['guard_playback_interrupt'](player)
                original = KeyboardInterrupt()
                with patch('wechat_player.guest') as input_command:
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        with guard(), guard():
                            raise original
                self.assertIs(raised.exception, original)
                player._image.assert_called_once()
                input_command.assert_not_called()

    def test_batch_continues_after_foreground_work_fails(self):
        import numpy as np
        from PIL import Image
        from wechat_player import Player, PlayerError
        source = entry['take_from_player']
        database = self.root / 'batch.sqlite3'
        with sqlite3.connect(database) as target:
            self.db.backup(target)
        second = 'https://weixin.qq.com/sph/second'
        with sqlite3.connect(database) as db:
            preview.record(db, second, dict(author='测试作者', published='2026-11-02', caption='第二部作品标题', cover_sha256='2'*64))
        allowed = self.root / 'authors.txt'
        allowed.write_text('测试作者\n')
        mock = Mock(spec=Player)
        mock.author = '测试作者'
        mock.size, mock.window = (1158, 960), 42
        mock.identity_box = (30, 838, 8, 4)
        mock.identity_regions = [(slice(0, 2), slice(None)), (slice(2, 4), slice(None))]
        picture = np.tile(np.array([0, 255] * 4, dtype='uint8'), (4, 1))
        mock.reference = [picture[r] > 70 for r in mock.identity_regions]
        mock.prepare.side_effect = PlayerError('first picture is unsupported')
        mock._image.return_value = (0.0, Image.fromarray(picture))
        rows = [dict(x=x, y=y, text=t) for x, y, t in (
            (100, 90, '测试作者'), (56, 140, '10条原创内容'), (106, 260, '直播回放'))]
        sidebar = [dict(r, x=r['x'] + 700) for r in rows] + [dict(x=94, y=888, text='测试作者')]
        next_take = save.__globals__['take_from_player']
        def take(db, item, folder):
            if item['fingerprint'] == self.item['fingerprint']:
                return source(db, item, folder)
            self.assertEqual(key.call_count, 1)
            self.assertIn('ctrl+w', key.call_args.args[2])
            self.assertEqual(key.call_args.args[-1], mock.window)
            mock._point.assert_not_called()
            self.assertEqual(ocr.call_count, 2)
            mock.return_to_author.assert_not_called()
            return next_take(db, item, folder)
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(preview, 'DATABASE', database))
            stack.enter_context(patch.dict(save.__globals__, AUTHORS=allowed, take_from_player=take, check_live_tools=lambda: None))
            stack.enter_context(patch('wechat_player.Player', return_value=mock))
            key = stack.enter_context(patch('wechat_player.guest'))
            ocr = stack.enter_context(patch('wechat_player.lines', side_effect=[sidebar, rows]))
            stack.enter_context(patch.object(sys, 'argv', ['wechat-save', 'save', self.urls[0], second]))
            stack.enter_context(redirect_stdout(output))
            self.assertEqual(entry['main'](), 1)
        first, following = (json.loads(line) for line in output.getvalue().splitlines())
        self.assertEqual(first['status'], 'needs_player')
        self.assertIn('first picture is unsupported', first['error'])
        self.assertEqual(following['status'], 'saved')
        self.assertEqual(self.starts, 1)

    def test_failure_does_not_navigate_after_focus_change_or_mismatch(self):
        import numpy as np
        from wechat_player import Player, PlayerError
        with self.subTest('changed window'):
            player = Mock(spec=Player)
            player.author = '测试作者'
            player.reference = [np.ones((2, 4), dtype=bool)] * 2
            player.identity_regions = [(slice(0, 2), slice(None)), (slice(2, 4), slice(None))]
            player.identity_box = (0, 0, 4, 4)
            player._image.side_effect = PlayerError('lost focus')
            initial = RuntimeError('original capture failure')
            with patch('wechat_player.guest') as key:
                with self.assertRaises(RuntimeError) as caught:
                    with entry['recover_failed_player'](player):
                        raise initial
            key.assert_not_called()
            self.assertIs(caught.exception, initial)
            player._pause.assert_not_called()
            player._point.assert_not_called()
            player.return_to_author.assert_not_called()
        for reference, status in ((None, 'needs_player'), ([object()], 'different_work')):
            with self.subTest(status=status):
                player = Mock(spec=Player)
                player.reference = reference
                err = entry['SaveError'](status, status)
                with self.assertRaises(entry['SaveError']) as caught:
                    with entry['recover_failed_player'](player):
                        raise err
                self.assertIs(caught.exception, err)
                player._inspect.assert_not_called()
                player._point.assert_not_called()

    def test_media_duration_and_observed_transition_are_bounded(self):
        def check(duration, elapsed, *, last=None, gap=0.375):
            entry['check_playback_timing'](duration, elapsed, dict(
                last_position=duration if last is None else last,
                end_monotonic=10.0, first_change_monotonic=10.0 + gap))
        check(14, 13.2, last=13)
        check(14, 15.1)
        check(52, 54.243, last=51)
        check(134, 132.316, last=133)
        check(224, 224.545, last=223)
        check(352, 354.167)
        for duration, elapsed, last, gap in [
                (14, 33.625, 14, 0.375), (14, 12.9, 14, 0.375), (52, 58, 52, 0.375),
                (134, 130.5, 133, 0.375),
                (14, 14, 12, 0.375), (224, 224, 224, 1.2), (1520, 1528.1, 1520, 0.375)]:
            with self.subTest(duration=duration, elapsed=elapsed, last=last, gap=gap):
                with self.assertRaises(entry['SaveError']) as caught:
                    check(duration, elapsed, last=last, gap=gap)
                self.assertEqual(caught.exception.status, 'incomplete_playback')

    def test_wrong_drive(self):
        with patch('wechat_paths.volume_uuid', return_value=('different-drive', self.root.parent)):
            with self.assertRaises(entry['SaveError']) as error:
                entry['verify_disk']()
        self.assertEqual(error.exception.status, 'disk_unavailable')

    def test_busy(self):
        with (self.root / 'save.lock').open('ab') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with self.assertRaises(entry['SaveError']) as error:
                with entry['exclusive_player']():
                    self.fail('lock was not exclusive')
            self.assertEqual(error.exception.exit_code, 75)


if __name__ == '__main__':
    unittest.main()
