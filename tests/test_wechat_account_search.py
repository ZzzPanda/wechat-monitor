import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_account_search import AccountSearch, READ_FINDER_ID, cards, rows_if_profile, tabs
from wechat_player import PlayerError


class WeChatAccountSearchTests(unittest.TestCase):
    def read_copied_address(self, address):
        # Exercise the same small stdlib-only extractor that runs inside the Linux container.
        output = io.StringIO()
        with patch('subprocess.run') as run, contextlib.redirect_stdout(output):
            run.return_value.stdout = address.encode()
            exec(READ_FINDER_ID, {})
        return output.getvalue().strip()

    def test_only_the_public_id_leaves_the_guest(self):
        url = 'https://channels.weixin.qq.com/web/pages/profile?username=public_id%40finder'
        self.assertEqual(self.read_copied_address(url + '&other=temporary'), 'public_id@finder')
        for invalid in (url + '&username=b%40finder', url.replace('profile?', 'work?'),
                        url.replace('.com/', '.com.example/'), url.replace('%40finder', '%2Fwork'),
                        '数字生命卡兹克'):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.read_copied_address(invalid), '')

    def test_keywords_match_card_names_only(self):
        def row(text, x, y):
            return dict(text=text, x=x, y=y)
        rows = [row('数字生命', 64, 98), row('数字生命卡兹克', 158, 308),
                row('数字生命相关科技', 158, 344), row('其他账号', 158, 476),
                row('讨论数字生命', 158, 512), row('数字生命地图', 158, 586)]
        self.assertEqual([hit['text'] for hit in cards(rows, '数字生命')], ['数字生命卡兹克', '数字生命地图'])
        self.assertEqual(cards(rows, '没人用的名字'), [])
        image = Image.new('RGB', (1280, 720))
        with patch('wechat_account_search.lines', return_value=rows):
            self.assertIsNone(rows_if_profile(image, '数字生命'))

    def test_collapsed_button_and_expanded_address_field(self):
        bar = Image.new('RGB', (1158, 65), '#eeeeef')
        draw = ImageDraw.Draw(bar)
        for left in (126, 207, 289, 370, 450):
            draw.line((left, 6, left+70, 6), fill='#e3e3e3')
        draw.rectangle((207, 8, 277, 40), fill='white')
        collapsed = bar.copy()
        ImageDraw.Draw(collapsed).line((531, 6, 562, 6), fill='#e3e3e3')
        mids, selected, address = tabs(collapsed)
        self.assertEqual((len(mids), selected, address), (5, 1, 547))

        draw.rectangle((531, 6, 920, 46), outline='#43cf87', fill='white')
        self.assertEqual(tabs(bar), (mids, 1, 726))
        with self.subTest('another active tab'):
            ambiguous = bar.copy()
            ImageDraw.Draw(ambiguous).rectangle((370, 8, 440, 40), fill='white')
            with self.assertRaisesRegex(PlayerError, '当前打开的标签页'):
                tabs(ambiguous)
        for side in ((531, 46, 920, 46), (531, 15, 531, 35), (920, 15, 920, 35)):
            damaged = bar.copy()
            ImageDraw.Draw(damaged).line(side, fill='white')
            with self.subTest(missing_border=side), self.assertRaisesRegex(PlayerError, '地址输入框'):
                tabs(damaged)

    def test_tab_bar_moves_off_hover_and_requires_a_unique_tab_in_a_fresh_frame(self):
        clear = Image.new('RGB', (1158, 720), '#eeeeef')
        draw = ImageDraw.Draw(clear)
        for left, width in ((126, 71), (207, 71), (288, 32)):
            draw.line((left, 6, left+width-1, 6), fill='#e3e3e3')
        draw.rectangle((126, 8, 196, 40), fill='white')
        hover = clear.copy()
        ImageDraw.Draw(hover).rectangle((207, 8, 277, 40), fill='white')
        search = AccountSearch('视频号', '读取主页上的公开作品')
        actions = Mock()
        search._point, search.image = actions.move, actions.capture
        search.image.return_value = clear
        with patch('wechat_account_search.time.sleep'):
            self.assertEqual(search.tab_bar(hover), tabs(clear))
            self.assertEqual(actions.mock_calls, [call.move(1030, 72), call.capture()])

            actions.reset_mock()
            search.image.return_value = hover
            with self.assertRaisesRegex(PlayerError, '当前打开的标签页'):
                search.tab_bar(hover)
            search._point.assert_called_once_with(1030, 72)
            self.assertEqual(search.image.call_count, 4)

            actions.reset_mock()
            blank = Image.new('RGB', clear.size, 'white')
            search.image.return_value = blank
            with self.assertRaisesRegex(PlayerError, '无法识别微信浏览器标签栏'):
                search.tab_bar(blank)
            search._point.assert_not_called()

    def test_reading_expanded_address_selects_the_whole_field(self):
        search = AccountSearch('视频号', '搜索可见的作者名片')
        search._point = Mock()
        search.press = Mock()
        with patch('wechat_account_search.guest', return_value=b'fixture@finder'), \
                patch('wechat_account_search.clipboard') as clipboard:
            self.assertEqual(search.address(bar=([180, 242], 1, 726)), 'fixture@finder')
        search._point.assert_called_once_with(726, 25, click=True)
        self.assertEqual(search.press.call_args_list, [call('ctrl+a', 'ctrl+c'), call('Escape')])
        self.assertEqual(clipboard.call_args_list, [call('wechat-profile-selection'), call('')])

    def test_search_discards_preclick_frames_and_falls_back_when_stream_ends(self):
        search = AccountSearch('视频号', '搜索可见的作者名片')
        stream = search.stream = Mock()
        stream.read.side_effect = [(99, 'old'), (101, 'current'), TimeoutError('No fresh image')]
        with patch('wechat_account_search.time.monotonic', return_value=100):
            self.assertEqual(search.image(), 'current')
            self.assertEqual(stream.read.call_count, 2)
            with patch.object(search, '_image', return_value=(102, 'single')) as grab:
                self.assertEqual(search.image(), 'single')
                self.assertIsNone(search.stream)
                grab.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
