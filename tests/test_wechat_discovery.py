"""Paging, pin handling and incremental boundaries using an isolated profile grid."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import wechat_discovery as discovery


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
        self.assertEqual(result, dict(complete=True, reason='微信显示列表到底，核实 6 条'))

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
