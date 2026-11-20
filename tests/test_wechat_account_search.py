import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_account_search import READ_FINDER_ID, cards, rows_if_profile


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
        with patch('wechat_account_search.lines', return_value=rows):
            self.assertEqual([hit['text'] for hit in cards(None, '数字生命')],
                             ['数字生命卡兹克', '数字生命地图'])
            self.assertEqual(cards(None, '没人用的名字'), [])
            self.assertIsNone(rows_if_profile(None, '数字生命'))


if __name__ == '__main__':
    unittest.main()
