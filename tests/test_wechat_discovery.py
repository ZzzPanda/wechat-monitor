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
    def traverse(self, pages, mode, known=(), *, end=True, failed=()):
        desktop = Mock()
        desktop.tab_bar.return_value = ([150, 200], 0, 300)
        index = 0
        im = Image.new('RGB', (1000, 650), 'white')
        desktop.image.return_value = im
        def scroll():
            nonlocal index
            index = min(index + 1, len(pages)-1)
        desktop.scroll.side_effect = scroll
        def cards(image):
            return [discovery.Tile((item % 5)*160, 500, str(item).encode()) for item in pages[index]]
        def open_work(card):
            item = int(card.signature)
            if item in failed:
                raise discovery.PlayerError('unavailable card')
            return f'url-{item}'
        desktop.open_work.side_effect = open_work
        def markers(crop):
            return [{'x':435, 'width':90, 'text':'没有更多了'}] if end and index==len(pages)-1 else []
        delivered = []
        with patch.object(discovery, 'check_drive'), patch.object(discovery, 'resolved_profile', return_value=('link','id')), \
             patch.object(discovery, 'ProfileDiscovery', return_value=desktop), \
             patch.object(discovery, 'tiles', side_effect=cards), patch.object(discovery, 'lines', side_effect=markers):
            result = discovery.crawl_profile({'name':'作者'}, mode=mode, known=known.__contains__,
                                             on_work=delivered.append, should_stop=lambda:False)
        self.assertEqual(desktop.open_work.call_count, desktop.return_to_grid.call_count)
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
