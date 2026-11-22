"""HTTP regression tests for the aggregated dashboard log."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

root = Path(__file__).resolve().parents[1]
module = root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'
sys.path.insert(0, str(module))
from server import DashboardServer, Library
from wechat_events import write_event


class LogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.monitoring = base/'monitoring'
        self.monitoring.mkdir()
        library = Library(self.monitoring/'authors.txt', base/'library.sqlite3', base/'media',
                          base/'posters', base/'static')
        self.server = DashboardServer(('127.0.0.1', 0), library, search=lambda _: [])
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, path='/api/logs', *, method='GET', data=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            client.request(method, path, body, request_headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def logs(self, query=''):
        status, headers, body = self.request('/api/logs' + query)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return json.loads(body)

    def test_category_and_inclusive_time_filters(self):
        beginning, ending = 1_700_000_000, 1_700_000_020
        fixtures = [(beginning - 1, 'system', 'info', '太早'),
                    (beginning, 'system', 'info', '开始部署'),
                    (beginning + 10, 'search', 'warning', '搜索响应较慢'),
                    (ending, 'crawl', 'error', '采集连接中断'),
                    (ending + 1, 'crawl', 'info', '太晚')]
        for when, category, level, message in fixtures:
            write_event(self.monitoring, category, level, message, when=when)

        query = f'?start={beginning}&end={ending}'
        expected = [dict(time=when, category=category, level=level, message=message)
                    for when, category, level, message in reversed(fixtures[1:-1])]
        self.assertEqual(self.logs(query + '&category=all'), dict(entries=expected, truncated=False))
        for index, category in enumerate(('crawl', 'search', 'system')):
            with self.subTest(category=category):
                self.assertEqual(self.logs(query + '&category=' + category)['entries'], [expected[index]])
        self.assertEqual(self.logs(f'?start={ending}&end={ending}')['entries'], expected[:1])

    def test_default_window_excludes_older_and_future_events(self):
        now = int(time.time())
        for when, message in ((now - 86_500, '早于过去一天'), (now - 60, '最近事件'), (now + 60, '将来事件')):
            write_event(self.monitoring, 'system', 'info', message, when=when)
        self.assertEqual(self.logs()['entries'], [dict(time=now - 60, category='system', level='info',
                                                       message='最近事件')])

    def test_invalid_filters_and_cross_site_requests_are_rejected(self):
        invalid = ('?category=other', '?category=', '?category=all&category=all', '?extra=1',
                   '?start=1', '?end=2', '?start=2&end=1', '?start=-1&end=2', '?start=1.2&end=2',
                   '?start=1&end=253402300800', '?start=1&start=1&end=2')
        for query in invalid:
            with self.subTest(query=query):
                self.assertEqual(self.request('/api/logs' + query)[0], 400)
        for headers in ({'Host': 'foreign.example.test'}, {'Origin': 'https://foreign.example.test'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.request(method='PUT', data={})[0], 405)

    def test_result_limit_is_applied_to_newest_entries(self):
        beginning = 1_700_000_000
        for offset in range(201):
            write_event(self.monitoring, 'system', 'info', f'事件 {offset}', when=beginning + offset)

        at_limit = self.logs(f'?category=system&start={beginning}&end={beginning + 199}')
        self.assertFalse(at_limit['truncated'])
        self.assertEqual(len(at_limit['entries']), 200)
        over_limit = self.logs(f'?category=all&start={beginning}&end={beginning + 200}')
        self.assertTrue(over_limit['truncated'])
        self.assertEqual([entry['time'] for entry in over_limit['entries']],
                         list(range(beginning + 200, beginning, -1)))

    def test_historic_crawl_and_settings_logs_omit_raw_private_values(self):
        now = int(time.time())
        profile_url = 'https://weixin.qq.com/f/private-profile'
        share_url = 'https://weixin.qq.com/sph/private-video'
        raw_error = 'worker stack trace with private path'
        with self.server.crawls.db(write=True) as db:
            db.executemany('INSERT INTO jobs(id,author,profile_url,mode,status,error,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?)',
                           [('finished', '测试视频号', profile_url, 'history', 'partial', raw_error, now - 2, now - 1),
                            ('running', '进行中的账号', profile_url, 'history', 'running', raw_error, now - 2, now)])
            db.executemany('INSERT INTO items(job_id,work_key,url,status,error) VALUES(?,?,?,?,?)',
                           [('finished', 'saved', share_url, 'saved', ''),
                            ('finished', 'failed', share_url, 'failed', raw_error)])

        model_settings = {'model_api_url': 'https://model.example.test/v1', 'model_name': 'private-model-id',
                          'model_api_key': 'fixture-private-key'}
        self.assertEqual(self.request('/api/settings', method='PUT', data=model_settings)[0], 200)
        result = self.logs()
        system, crawl = result['entries']
        self.assertEqual((system['category'], system['level']), ('system', 'info'))
        self.assertIn('设置', system['message'])
        self.assertEqual((crawl['time'], crawl['category'], crawl['level']), (now - 1, 'crawl', 'warning'))
        for text in ('测试视频号', '历史采集', '部分完成', '发现 2 条', '保存 1 条', '失败 1 条'):
            self.assertIn(text, crawl['message'])
        serialized = json.dumps(result, ensure_ascii=False)
        for value in (profile_url, share_url, raw_error, '进行中的账号', *model_settings.values()):
            self.assertNotIn(value, serialized)


if __name__ == '__main__':
    unittest.main()
