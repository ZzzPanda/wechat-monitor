"""Regression cases for public-account list paging and multi-article rows."""
from contextlib import nullcontext
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wechat_public_discovery as public


class ArticleRowsTests(unittest.TestCase):
    def test_number_of_pinned_items_needs_its_section_label(self):
        frame = Image.new('RGB', (1158, 960))
        heading = dict(text='置顶', x=9, y=191, width=34, height=20)
        count = dict(text='2个内容', x=21, y=242, width=63, height=20)
        with patch.object(public, 'lines', return_value=[heading, count]):
            self.assertEqual(public.collapsed_pins(frame), (2, 789, 397))
        with patch.object(public, 'lines', return_value=[count]):
            self.assertIsNone(public.collapsed_pins(frame))

    def test_headline_and_all_subarticles_survive_missing_or_duplicate_ocr(self):
        image = Image.new('RGB', (1158, 960), (237, 237, 237))
        draw = ImageDraw.Draw(image)
        left = 334
        draw.rectangle((left+1, 220, left+490, 801), fill='white')
        draw.rectangle((left+1, 250, left+490, 458), fill='#626a6c')
        for y in (480, 600, 720):
            draw.rectangle((left+391, y, left+470, y+79), fill='#324254')
        # Reading count only recognized on subarticle 2. The other three have
        # valid thumbnails/cover, including the white-on-photo headline.
        rows = [dict(text='阅读1.1万 赞66', x=14, y=660-145)]
        with patch.object(public, 'lines', return_value=rows):
            found = public.article_rows(image)
        self.assertEqual(len(found), 4)
        self.assertTrue(250 < found[0].y < 459)
        self.assertTrue(480 <= found[1].y < 560)
        self.assertTrue(600 <= found[2].y < 680)
        self.assertTrue(720 <= found[3].y < 800)

    def test_wide_crop_recovers_white_reading_counter_misread_on_cover(self):
        image = Image.new('RGB', (1158, 960), 'white')
        def count_lines(crop):
            if crop.width == 490:
                return [dict(text='阅读2.1万 赞571', x=20, y=539)]
            return [dict(text='肉滨2i片路57', x=14, y=539)]
        with patch.object(public, 'lines', side_effect=count_lines):
            self.assertEqual([r.y for r in public.article_rows(image)], [668])

    def test_plain_article_hit_never_becomes_public_account(self):
        frame = Image.new('RGB', (1158, 960))
        row = dict(text='三联生活周刊©', x=18, y=32, width=130, height=24)
        tag = dict(text='公众号', x=21, y=63, width=52, height=21)
        with patch.object(public, 'lines', return_value=[row, tag]):
            self.assertIsNotNone(public.search_hit(frame, '三联生活周刊'))
            self.assertIsNone(public.search_hit(frame, '三联'))
        with patch.object(public, 'lines', return_value=[row, dict(tag, text='视频号')]):
            self.assertIsNone(public.search_hit(frame, '三联生活周刊'))


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.account = dict(name='周刊', biz='MTc5MTU3NTYyMQ==')
        self.browser = Mock()
        self.browser.profile.return_value = nullcontext(self.account)
        self.browser.expand_pinned.return_value = 0
        self.browser.article_url.side_effect = lambda row: row.link
        self.archive = Mock(return_value=False)
        for target, options in [('PublicProfile', dict(return_value=self.browser)), ('check_drive', {})]:
            p = patch.object(public, target, **options)
            p.start()
            self.addCleanup(p.stop)

    def run_job(self, pages, mode='history', **kwargs):
        with patch.object(public, 'history_pages', return_value=iter(pages)):
            return public.crawl_public_account(self.account, mode=mode, on_article=self.archive,
                                               should_stop=lambda: False, **kwargs)

    @staticmethod
    def page(n, labels, *, pinned=False, end=False, stalled=False, gaps=(900,)):
        rows = [Mock(y=250+index*150, link=label) for index, label in enumerate(labels)]
        return n, rows, 'digest'+str(n), end, stalled, not pinned, gaps

    def test_page_limit_preserves_secondary_and_overlap_dedup(self):
        result = self.run_job([self.page(0, ['head1','sub1']), self.page(1, ['sub1','sub2'])], max_pages=2)
        self.assertTrue(result['complete'] and result['bounded'])
        self.assertEqual(self.archive.call_args_list, [call('head1'),call('sub1'),call('sub2')])

    def test_limit_inside_push_finishes_remaining_siblings_only(self):
        pages = [self.page(0, ['earlier', 'head'], gaps=(330,)),
                 self.page(1, ['earlier', 'head', 'sub2', 'sub3', 'next'], gaps=(330, 770))]
        result = self.run_job(pages, max_pages=1)
        self.assertTrue(result['complete'] and result['bounded'])
        self.assertEqual(result['pages'], 2)
        self.assertEqual(self.archive.call_args_list, [call('earlier'), call('head'), call('sub2'), call('sub3')])

    def test_pinned_only_screen_does_not_consume_requested_timeline_range(self):
        self.browser.expand_pinned.return_value = 2
        pages = [self.page(0, ['pin1', 'pin2'], pinned=True),
                 self.page(1, ['pin2', 'recent1', 'recent2'])]
        result = self.run_job(pages, max_pages=1)
        self.assertTrue(result['complete'])
        self.assertEqual(result['pages'], 2)
        self.assertEqual(self.archive.call_args_list, [call('pin1'), call('pin2'), call('recent1'), call('recent2')])

    def test_unlocated_clipped_push_is_not_complete_or_extended_into_neighbours(self):
        pages = [self.page(0, ['head'], gaps=()), self.page(1, ['unrelated'], end=True)]
        self.assertFalse(self.run_job(pages, max_pages=1)['complete'])
        self.archive.assert_called_once_with('head')

    def test_incremental_needs_distinct_old_content_after_pinned_and_checks_all_siblings(self):
        self.archive.side_effect = lambda url: url != 'new'
        pages = [self.page(0,['pinned-a','pinned-b'],pinned=True),
                 self.page(1,['pinned-b','pinned-c'],pinned=True),
                 self.page(2,['old1','new']), self.page(3,['new','old2']),
                 self.page(4,['old2','old3']), self.page(5,['old3','old4'])]
        result = self.run_job(pages, mode='incremental')
        self.assertEqual(result['pages'], 6)
        self.assertIn(call('new'), self.archive.call_args_list)
        self.assertTrue(result['complete'])

    def test_incremental_finishes_the_last_open_push(self):
        self.archive.return_value = True
        pages = [self.page(0, ['first']),
                 self.page(1, ['earlier', 'head'], gaps=(330,)),
                 self.page(2, ['earlier', 'head', 'secondary', 'last', 'next'], gaps=(330, 770))]
        result = self.run_job(pages, mode='incremental')
        self.assertTrue(result['complete'])
        self.assertEqual(self.archive.call_args_list,
                         [call('first'), call('earlier'), call('head'), call('secondary'), call('last')])

    def test_unopened_secondary_and_unconfirmed_bottom_are_partial(self):
        def inaccessible(row):
            if row.link=='lost':
                raise public.PlayerError('文章已失效。')
            return row.link
        self.browser.article_url.side_effect = inaccessible
        with self.assertLogs(level='WARNING'):
            result = self.run_job([self.page(0,['head','lost','sub3'],end=True)])
        self.assertEqual(self.archive.call_args_list,[call('head'),call('sub3')])
        self.assertFalse(result['complete'])
        result = self.run_job([self.page(0,[],stalled=True)])
        self.assertFalse(result['complete'])

    def test_history_releases_at_boundary_and_only_skips_verified_prefix(self):
        yielded = self.run_job([self.page(0,['one'])],on_boundary=lambda checkpoint: True)
        self.assertTrue(yielded['yielded'])
        self.archive.reset_mock()
        pages = [self.page(0,['one']),self.page(1,['two'],end=True)]
        result = self.run_job(pages, checkpoint=dict(v=1,next_page=1,prefix='digest0'))
        self.archive.assert_called_once_with('two')
        self.assertTrue(result['complete'])
        self.archive.reset_mock()
        with patch.object(public,'history_pages',side_effect=lambda profile: iter(pages)), patch.object(public.time,'sleep'):
            result = public.crawl_public_account(self.account,mode='history',on_article=self.archive,
                                                 should_stop=lambda:False,
                                                 checkpoint=dict(v=1,next_page=1,prefix='old-digest'))
        self.assertTrue(result['complete'])
        self.assertEqual(self.archive.call_args_list,[call('one'),call('two')])
