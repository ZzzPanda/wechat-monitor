"""Paging, pin handling and incremental boundaries using an isolated profile grid."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import wechat_discovery as discovery


class ProfileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.page = discovery.ProfileDiscovery('测试作者')
        self.page.image = Mock()
        self.page.address = Mock(return_value='target@finder')
        self.page._point = Mock()
        self.page.tab_bar = Mock(return_value=([150], 0, 220))
        self.page.press = Mock()
        self.page.wait = Mock()
        self.frame = Image.new('RGB', (1158, 960))
        self.rows = [dict(x=x, y=y, width=70, height=22, text=text) for x, y, text in (
            (204, 301, '喔唷，崩溃啦！'), (202, 356, '显示此网页时出了点问题。'),
            (204, 402, '错误代码：9'), (862, 458, '重新加载'), (202, 460, '了解详情'))]
        self.url = discovery.PROFILE + '?username=target%40finder'
        for obj, key, options in [(discovery, 'windows', {'return_value': ['window']}),
                                  (discovery, 'guest', {}), (discovery.time, 'sleep', {})]:
            mocked = patch.object(obj, key, **options)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_only_confirmed_crash_page_can_reload(self):
        for rows, expected in (
                (self.rows, ('9', self.rows[3])),
                (self.rows + [self.rows[3]], ('9', None)),
                (self.rows[:3], ('9', None)),
                (self.rows[1:], None),
                ([dict(self.rows[0], text='请完成安全验证'), self.rows[3]], None)):
            with self.subTest(rows=rows), patch.object(discovery, 'lines', return_value=rows):
                self.assertEqual(discovery.renderer_crash(self.frame), expected)

    def test_crash_before_navigation_is_reloaded_once_and_identity_verified(self):
        self.page.image.return_value = self.frame
        with patch.object(discovery, 'lines', return_value=self.rows), self.assertLogs(level='WARNING'):
            self.page.open_profile(self.url, 'target@finder')
        self.page._point.assert_called_once_with(897, 469, click=True)
        self.assertEqual(self.page.address.call_args_list, [call(self.url, bar=self.page.tab_bar.return_value), call()])
        self.assertEqual(self.page.profile_id, 'target@finder')

    def test_crash_after_navigation_retries_original_url_and_rejects_wrong_account(self):
        self.page.image.return_value = self.frame
        self.page.wait.side_effect = [discovery.PlayerError('未打开主页'), True, True]
        self.page.address.return_value = 'another@finder'
        with patch.object(discovery, 'lines', side_effect=[[], self.rows]), self.assertLogs(level='WARNING'):
            with self.assertRaisesRegex(discovery.PlayerError, '标识不一致'):
                self.page.open_profile(self.url, 'target@finder')
        self.page._point.assert_called_once()
        navigate = call(self.url, bar=self.page.tab_bar.return_value)
        self.assertEqual(self.page.address.call_args_list, [navigate, navigate, call()])
        self.page.press.assert_not_called()

    def test_persistent_crash_reports_error_code_and_stops_after_one_reload(self):
        self.page.image.return_value = self.frame
        def no_recovery(read, error, seconds):
            self.assertFalse(read(self.frame))
            raise discovery.PlayerError(error)
        self.page.wait.side_effect = no_recovery
        with patch.object(discovery, 'lines', return_value=self.rows), self.assertLogs(level='WARNING'):
            with self.assertRaisesRegex(discovery.PlayerError, '错误代码 9.*仍显示崩溃页'):
                self.page.open_profile(self.url, 'target@finder')
        self.page._point.assert_called_once()
        self.page.address.assert_not_called()

    def test_crash_recovery_preserves_navigation_error_if_the_tab_strip_is_unreadable(self):
        bar = self.page.tab_bar.return_value
        self.page.image.return_value = self.frame
        self.page.wait.side_effect = [discovery.PlayerError('原始导航超时'), True, True]
        self.page.tab_bar.side_effect = [bar, OSError('temporarily unreadable'), bar, bar, bar]
        with patch.object(discovery, 'lines', side_effect=[[], self.rows]), self.assertLogs(level='WARNING'):
            self.page.open_profile(self.url, 'target@finder')
        self.page._point.assert_called_once()
        self.assertEqual(self.page.profile_id, 'target@finder')
        self.assertFalse(self.page.opened_profile_tabs)

    def test_other_load_failure_is_not_retried(self):
        self.page.image.return_value = self.frame
        self.page.wait.side_effect = discovery.PlayerError('微信未打开指定作者的主页。')
        with patch.object(discovery, 'lines', return_value=[self.rows[3]]):
            with self.assertRaisesRegex(discovery.PlayerError, '未打开指定作者'):
                self.page.open_profile(self.url, 'target@finder')
        self.page._point.assert_not_called()
        self.page.address.assert_called_once_with(self.url, bar=self.page.tab_bar.return_value)

    def test_owned_navigation_tabs_are_released_in_reverse_and_originals_preserved(self):
        old = ([160, 320], 0, 680)
        opened = ([160, 300, 440], 2, 680)
        nested = ([160, 260, 360, 460], 3, 680)
        self.page.tab_bar.side_effect = [old, opened, opened, nested]
        self.page._navigate(self.url, 'timeout')
        self.page.wait.side_effect = discovery.PlayerError('second navigation failed')
        with self.assertRaisesRegex(discovery.PlayerError, 'second navigation failed'):
            self.page._navigate(self.url, 'timeout')
        self.page.tab_bar.side_effect = [nested, nested, opened, opened, opened, old]
        self.page.close_profile_tabs()
        self.assertEqual(self.page.press.call_args_list, [call('ctrl+w'), call('ctrl+w')])
        self.assertEqual(self.page.opened_profile_tabs, [])

    def test_cleanup_preserves_existing_or_changed_tabs(self):
        old = ([160, 320], 1, 680)
        other = ([160, 300, 440], 0, 680)
        target = ([160, 300, 440], 2, 680)
        self.page.tab_bar.side_effect = [old, old]
        self.page._navigate(self.url, 'timeout')
        self.page.close_profile_tabs()
        self.page.press.assert_not_called()
        for layout in (other, target):
            self.page.tab_bar.side_effect = [old, target, layout]
            self.page._navigate(self.url, 'timeout')
            self.page.profile_id, self.page.profile_tab = 'target@finder', 2
            self.page.address.return_value = 'different@finder'
            self.page.close_profile_tabs()
            self.page.press.assert_not_called()

    def test_reentering_the_same_verified_profile_rewinds_in_place(self):
        self.page.profile_id, self.page.profile_tab = 'target@finder', 0
        self.page.image.return_value = self.frame
        with patch.object(discovery, 'renderer_crash', return_value=None):
            self.page.open_profile(self.url, 'target@finder')
        self.page.press.assert_called_once_with('Home')
        self.page.address.assert_called_once_with()
        self.assertFalse(self.page.opened_profile_tabs)


class WorkLinkTests(unittest.TestCase):
    def setUp(self):
        self.page = discovery.ProfileDiscovery('测试作者')
        self.page.size = (1000, 650)
        self.page._point = Mock()
        self.page._pause = Mock()
        self.url = 'https://weixin.qq.com/sph/test'
        self.page.copy_visible_share_url = Mock(return_value=self.url)
        self.card = discovery.Tile(300, 350, b'cover')
        frame = Image.new('RGB', self.page.size)
        self.author = dict(x=70, y=600, width=100, height=20, text='测试作者')
        self.caption = dict(x=32, y=550, width=160, height=20, text='这张作品卡的标题')
        self.before = (frame, self.author, self.caption)

        def wait(read, message, seconds):
            result = read(frame)
            if not result:
                raise discovery.PlayerError(message)
            return result
        self.page.wait = wait

    def test_share_works_without_a_recognizable_track(self):
        with patch.object(discovery, 'footer', return_value=self.before):
            self.assertEqual(self.page.open_work(self.card), self.url)
        self.page.copy_visible_share_url.assert_called_once_with(self.before[0], self.author)
        self.page._pause.assert_not_called()

    def test_rejects_autoplay_changing_the_work_or_author_during_copy(self):
        changed = (self.before[0], self.author, dict(self.caption, text='作者发的另一条作品'))
        longer = (self.before[0], self.author, dict(self.caption, text=self.caption['text']+'续集'))
        for after in (changed, longer, None):
            with self.subTest(after=after), patch.object(discovery, 'footer', side_effect=[self.before, after]):
                with self.assertRaisesRegex(discovery.PlayerError, '同一作者和作品'):
                    self.page.open_work(self.card)
        self.page._pause.assert_not_called()

    def test_subtitle_merged_with_native_caption_requires_exact_local_reread(self):
        merged = dict(self.caption, y=524, height=46, width=530, text='播放器字幕和其下的原生标题')
        after = (self.before[0], self.author, merged)
        for text in (self.caption['text']+'！', '作者发的另一条作品'):
            with self.subTest(text=text), patch.object(discovery, 'footer', side_effect=[self.before, after]), \
                    patch.object(discovery, 'lines', return_value=[{'text':text}]) as read:
                if text.endswith('！'):
                    self.assertEqual(self.page.open_work(self.card), self.url)
                else:
                    with self.assertRaisesRegex(discovery.PlayerError, '同一作者和作品'):
                        self.page.open_work(self.card)
                self.assertEqual(read.call_args.args[0].size, (344, 56))

    def test_unreadable_caption_still_requires_verified_pause_and_author(self):
        before = (self.before[0], self.author, None)
        with patch.object(discovery, 'footer', side_effect=[before, None]):
            with self.assertRaisesRegex(discovery.PlayerError, '再次核验作者'):
                self.page.open_work(self.card)
        self.page._pause.assert_called_once()
        self.page.copy_visible_share_url.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def traverse(self, pages, mode, known=(), *, end=True, failed=(), checkpoint=None, on_boundary=None):
        desktop = Mock()
        desktop.tab_bar.return_value = ([150, 200], 0, 300)
        index = 0
        in_player = False
        im = Image.new('RGB', (1000, 650), 'white')
        desktop.image.return_value = im
        def open_profile(*args):
            nonlocal index
            index = 0
        desktop.open_profile.side_effect = open_profile
        def scroll():
            nonlocal index
            index = min(index + 1, len(pages)-1)
        desktop.scroll.side_effect = scroll
        def cards(image):
            return [discovery.Tile((item % 5)*160, 500, str(item).encode()) for item in pages[index]]
        def open_work(card):
            nonlocal in_player
            self.assertFalse(in_player)
            in_player = True
            item = int(card.signature)
            if item in failed:
                raise discovery.PlayerError('unavailable card')
            return f'url-{item}'
        desktop.open_work.side_effect = open_work
        def return_to_grid(*args):
            nonlocal in_player
            self.assertTrue(in_player)
            in_player = False
        desktop.return_to_grid.side_effect = return_to_grid

        def boundary(cursor):
            self.assertFalse(in_player)
            return on_boundary(cursor)
        def markers(crop):
            return [{'x':435, 'width':90, 'text':'没有更多了'}] if end and index==len(pages)-1 else []
        delivered = []
        with patch.object(discovery, 'check_drive'), patch.object(discovery, 'resolved_profile', return_value=('link','id')), \
             patch.object(discovery, 'ProfileDiscovery', return_value=desktop), \
             patch.object(discovery, 'tiles', side_effect=cards), patch.object(discovery, 'lines', side_effect=markers):
            result = discovery.crawl_profile({'name':'作者'}, mode=mode, known=known.__contains__,
                                             on_work=delivered.append, should_stop=lambda:False, checkpoint=checkpoint,
                                             on_boundary=boundary if on_boundary else None)
        self.assertEqual(desktop.open_work.call_count, desktop.return_to_grid.call_count)
        desktop.close_profile_tabs.assert_called_once_with()
        self.assertFalse(in_player)
        return delivered, result

    def test_baseline_includes_enough_preexisting_works_for_incremental_boundary(self):
        pages = [list(range(5)), list(range(3, 10)), list(range(8, 15))]
        baseline, report = self.traverse(pages, 'baseline')
        self.assertEqual(baseline, [f'url-{i}' for i in range(10)])
        self.assertTrue(report['complete'])
        delivered, report = self.traverse(pages, 'incremental', set(baseline))
        self.assertEqual(delivered, [])
        self.assertTrue(report['complete'])
        delivered, report = self.traverse([[0, 1, 2, 25, 3], [3, 4, 5, 6, 7]], 'incremental', set(baseline))
        self.assertEqual(delivered, ['url-25'])
        self.assertTrue(report['complete'])

    def test_history_delivers_known_and_unknown_once_across_overlapping_pages(self):
        delivered, report = self.traverse([[1, 2], [2, 3, 4]], 'history', {'url-2'})
        self.assertEqual(delivered, ['url-1','url-2','url-3','url-4'])
        self.assertTrue(report['complete'])
        delivered, report = self.traverse([[1, 2]], 'baseline')
        self.assertTrue(report['complete'])
        self.assertEqual(delivered, ['url-1','url-2'])

    def test_unverified_card_breaks_the_run_of_known_works(self):
        pages = [[0, 1, 2, 3, 4], [4, 5, 6, 7]]
        known = {f'url-{n}' for n in range(7)}
        with self.assertLogs(level='WARNING'):
            delivered, report = self.traverse(pages, 'incremental', known, failed=[5])
        self.assertEqual(delivered, ['url-7'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['unverified_cards'], 1)

    def test_two_yields_resume_mid_view_and_at_the_end_of_a_view(self):
        pages = [[1, 2], [2, 3, 4], [4, 5, 6]]
        cursor = {}
        def yield_after(n):
            def boundary(progress):
                if progress['processed'] == n:
                    cursor.update(progress)
                    return True
                return False
            return boundary
        delivered, result = self.traverse(pages, 'history', on_boundary=yield_after(3))
        self.assertTrue(result['yielded'])
        self.assertEqual(delivered, ['url-1', 'url-2', 'url-3'])
        delivered, result = self.traverse(pages, 'history', checkpoint=cursor.copy(), on_boundary=yield_after(4))
        self.assertTrue(result['yielded'])
        self.assertEqual(delivered, ['url-4'])
        delivered, result = self.traverse(pages, 'history', checkpoint=cursor)
        self.assertEqual(delivered, ['url-5', 'url-6'])
        self.assertEqual(result, dict(complete=True, reason='微信显示列表到底，核实 6 条', unverified_cards=0))

    def test_changed_middle_view_invalidates_the_old_prefix(self):
        pages = [[1, 2], [3, 4], [5, 6]]
        saved = []
        def after_five(cursor):
            if cursor['processed'] == 5:
                saved.append(cursor)
                return True
            return False
        self.traverse(pages, 'history', on_boundary=after_five)
        delivered, result = self.traverse([[1, 2], [30, 4], [5, 6]], 'history', checkpoint=saved[0])
        self.assertEqual(delivered, ['url-1','url-2','url-30','url-4','url-5','url-6'])
        self.assertTrue(result['complete'])

    def test_fast_forward_can_yield_again_without_losing_original_checkpoint(self):
        saved = []
        def yield_at_third_view(cursor):
            if cursor['page'] == 2:
                saved.append(cursor)
                return True
            return False
        pages = [[1, 2], [3, 4], [5, 6]]
        self.traverse(pages, 'history', on_boundary=yield_at_third_view)
        old = saved[0].copy()
        delivered, result = self.traverse(pages, 'history', checkpoint=old,
                                          on_boundary=lambda cursor: cursor == old)
        self.assertTrue(result['yielded'])
        self.assertEqual(delivered, [])
        delivered, result = self.traverse(pages, 'history', checkpoint=old)
        self.assertEqual(delivered, ['url-5','url-6'])
        never = Mock(return_value=True)
        self.traverse(pages, 'incremental', checkpoint=old, on_boundary=never)
        never.assert_not_called()

    def test_early_card_errors_are_reported_after_a_resume(self):
        saved = []
        def boundary(cursor):
            if cursor['failures']:
                saved.append(cursor)
                return True
            return False
        with self.assertLogs(level='WARNING'):
            self.traverse([[1, 2]], 'history', failed=[1], on_boundary=boundary)
        delivered, result = self.traverse([[1, 2]], 'history', checkpoint=saved[0])
        self.assertEqual(delivered, ['url-2'])
        self.assertFalse(result['complete'])
        self.assertIn('1 张作品卡', result['reason'])
        with self.assertLogs(level='WARNING'):
            _, changed = self.traverse([[1, 3]], 'history', checkpoint=saved[0], failed=[1])
        self.assertIn('1 张作品卡', changed['reason'])

    def test_unconfirmed_end_and_failed_card_are_reported(self):
        delivered, report = self.traverse([[1, 2]], 'history', end=False)
        self.assertFalse(report['complete'])
        self.assertEqual(delivered, ['url-1','url-2'])
        with self.assertLogs(level='WARNING') as output:
            delivered, report = self.traverse([[1, 2]], 'history', failed=[1])
        self.assertFalse(report['complete'])
        self.assertEqual(delivered, ['url-2'])
        self.assertIn('unavailable card', report['reason'])
        self.assertIn('unavailable card', output.output[0])

    def test_thumbnail_edge_detects_last_partial_row(self):
        image = Image.new('RGB', (1000, 700), 'white')
        draw = ImageDraw.Draw(image)
        for left in (60, 260, 460):
            draw.rounded_rectangle((left, 220, left+178, 460), 5, fill='#777')
        cards = discovery.tiles(image)
        self.assertEqual(len(cards), 3)
        self.assertTrue(all(c.y > 350 for c in cards))

    def test_identical_cover_on_a_different_work_is_not_an_overlap(self):
        old = discovery.Tile(140, 500, b'shared-cover')
        new = discovery.Tile(140, 250, b'shared-cover')
        rng = np.random.default_rng(17)
        image = rng.integers(0, 255, (700, 80), dtype='int16')
        moved = np.roll(image, -250, axis=0)
        self.assertEqual(discovery.overlapping_tiles([new], [old], image, moved), {new})
        self.assertEqual(discovery.overlapping_tiles([new], [old], image, image), set())


if __name__ == '__main__':
    unittest.main()
