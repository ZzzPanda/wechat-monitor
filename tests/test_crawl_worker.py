"""Drive the real collection worker, HTTP API and durable queue with a fixture WeChat grid."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'),
                str(root/'scripts')]
import crawl_worker
from server import DashboardServer, Library
from wechat_events import read_events
import wechat_preview


class CrawlWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        lib = Library(base/'authors.txt', base/'library.sqlite3', base/'media', base/'posters', base/'static')
        lib.add_author(dict(name='测试作者', url='https://weixin.qq.com/f/fixture'))
        self.entry = lib.entries()[0]
        self.server = DashboardServer(('127.0.0.1', 0), lib)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        (base/'crawl-worker.key').write_text('isolated-worker-credential-for-tests-only')
        endpoint = 'http://%s:%d/api/crawl-worker' % self.server.server_address
        for key, value in (('ENDPOINT', endpoint), ('MONITORING', base)):
            replacement = patch.object(crawl_worker, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.api = crawl_worker.Dashboard()
        self.queue = self.server.crawls
        self.downloads = sqlite3.connect(base/'fixture-media.sqlite3')
        self.addCleanup(self.downloads.close)
        self.downloads.execute('CREATE TABLE saved(fingerprint TEXT PRIMARY KEY)')
        self.saved = []
        self.posters = []
        self.fail = set()
        self.urls = {}
        self.dates = {}
        self.modes = []
        self.after_save = None
        self.saver = dict(work=self.work, save=self.save, SaveError=RuntimeError,
                          update_poster=self.posters.append, preview=wechat_preview)

    def share(self, key, code=None):
        url = 'https://weixin.qq.com/sph/' + (code or key)
        self.urls[url] = key
        return url

    def work(self, db, url, *, fetch):
        self.assertTrue(fetch)
        return dict(author=self.entry['name'], fingerprint=self.urls[url],
                    published=self.dates.get(url), caption=self.urls[url] + '的视频')

    def save(self, db, url, item, *, return_to_profile):
        self.assertFalse(return_to_profile)
        key = item['fingerprint']
        self.saved.append(key)
        if key in self.fail:
            raise RuntimeError('模拟播放器失败')
        with db:
            added = db.execute('INSERT OR IGNORE INTO saved VALUES(?)', (key,)).rowcount
        if self.after_save:
            self.after_save()
        return dict(status='saved' if added else 'already_saved', work_key=key)

    def run_grid(self, requested, urls, *, complete=True):
        job_id = self.queue.enqueue(self.entry, requested)['id']
        job = self.api('claim')['job']
        self.assertEqual(job['id'], job_id)
        self.active_job = job_id

        def discover(entry, *, mode, known, on_work, should_stop, checkpoint=None, on_boundary=None):
            self.assertEqual((entry['name'], entry['url']), (self.entry['name'], self.entry['url']))
            self.modes.append(mode)
            for url in urls:
                if should_stop():
                    return dict(complete=False, reason='已停止')
                if mode != 'incremental' or not known(url):
                    on_work(url)
            return dict(complete=complete, reason='模拟微信列表末尾' if complete else '有一张作品卡未核验')

        crawl_worker.run_job(self.api, job, self.saver, self.downloads, discover)
        return next(row for row in self.queue.snapshot()['jobs'] if row['id'] == job_id)

    def test_service_logs_one_outage_and_recovery(self):
        client = Mock(side_effect=[
            {}, crawl_worker.ServiceError('private transport detail'),
            crawl_worker.ServiceError('still unavailable'), {'queued': []},
        ])
        with patch.object(crawl_worker, 'Dashboard', return_value=client), \
                patch.object(crawl_worker.time, 'sleep', side_effect=[None, None, InterruptedError]), \
                patch('builtins.print'):
            with self.assertRaises(InterruptedError):
                crawl_worker.serve()
        events = read_events(crawl_worker.MONITORING, 'crawl', 0, 2**63 - 1, 10)
        self.assertEqual([(event['level'], event['message']) for event in events], [
            ('info', '采集调度已恢复。'),
            ('error', '采集进程暂时无法联系管理页，稍后重试。'),
        ])

    def test_hourly_round_runs_between_two_history_saves_then_resumes_same_job(self):
        old, new, first, second = [self.share(key) for key in ('old', 'new', 'first', 'second')]
        self.run_grid('incremental', [old])
        self.server.library.add_author(dict(name='第二个账号', url='https://weixin.qq.com/f/second'))
        history = self.queue.enqueue(self.entry)['id']
        job = self.api('claim')['job']
        self.assertEqual(job['id'], history)
        positions = []

        def history_grid(entry, *, mode, known, on_work, should_stop, checkpoint, on_boundary):
            offset = checkpoint['offset'] if checkpoint else 0
            positions.append(offset)
            for i in range(offset, 2):
                if on_boundary({'offset': i}):
                    return {'yielded': True}
                on_work((first, second)[i])
            return dict(complete=True, reason='列表到底')

        def time_reached_during_save():
            self.after_save = None
            scheduled = self.queue.schedule(self.server.library.entries(), now=self.queue.snapshot()['settings']['next_run'])
            self.assertEqual(len(scheduled), 2)
            current = next(j for j in self.queue.snapshot()['jobs'] if j['id'] == history)
            self.assertEqual((current['status'], current['saved']), ('running', 0))

        self.after_save = time_reached_during_save
        crawl_worker.run_job(self.api, job, self.saver, self.downloads, history_grid)
        waiting = next(j for j in self.queue.snapshot()['jobs'] if j['id'] == history)
        self.assertEqual((waiting['status'], waiting['saved'], self.saved), ('queued', 1, ['first']))
        self.assertEqual(self.queue.enqueue(self.entry)['id'], history)

        def incremental_grid(entry, *, mode, known, on_work, should_stop):
            if entry['name'] == self.entry['name']:
                self.assertFalse(known(new))
                on_work(new)
                return dict(complete=True, reason='增量检查结束')
            return dict(complete=False, reason='作品卡未能核验', unverified_cards=1)

        accounts = []
        for _ in range(2):
            increment = self.api('claim')['job']
            self.assertEqual(increment['mode'], 'incremental')
            accounts.append(increment['author'])
            crawl_worker.run_job(self.api, increment, self.saver, self.downloads, incremental_grid)
        self.assertCountEqual(accounts, [self.entry['name'], '第二个账号'])
        peer_job = next(j for j in self.queue.snapshot()['jobs'] if j['author'] == '第二个账号')
        self.assertEqual((peer_job['status'], peer_job['failed'], peer_job['unverified_cards']), ('partial', 0, 1))
        log = next(row for row in self.queue.log_entries(0, 2**63-1, 10) if '第二个账号' in row['message'])
        self.assertIn('1 张作品卡未能核验', log['message'])
        self.assertNotIn('失败 0 条', log['message'])
        resumed = self.api('claim')['job']
        self.assertEqual((resumed['id'], resumed['saved']), (history, 1))
        crawl_worker.run_job(self.api, resumed, self.saver, self.downloads, history_grid)
        result = next(j for j in self.queue.snapshot()['jobs'] if j['id'] == history)
        self.assertEqual((result['status'], result['saved'], positions), ('done', 2, [0, 1]))
        self.assertEqual(self.saved, ['first', 'new', 'second'])

    def test_first_incremental_baselines_then_only_new_fingerprints_are_saved(self):
        old = self.share('old')
        old_reissued = self.share('old', 'reroutedOldLink')
        new = self.share('new')
        baseline = self.run_grid('incremental', [old])
        self.assertEqual((self.modes[-1], baseline['status'], baseline['skipped']), ('baseline', 'done', 1))
        self.assertEqual(self.saved, [])
        quiet = self.run_grid('incremental', [old_reissued])
        self.assertEqual((self.modes[-1], quiet['saved'], self.saved), ('incremental', 0, []))
        added = self.run_grid('incremental', [new, old_reissued])
        self.assertEqual((added['saved'], self.saved), (1, ['new']))
        self.run_grid('incremental', [self.share('new','sameNewWork'), old])
        self.assertEqual(self.saved, ['new'])
        history = self.run_grid('history', [new, old])
        self.assertEqual((history['status'], history['saved'], history['skipped']), ('done', 1, 1))
        self.assertEqual(self.posters, ['new', 'old'])

    def test_partial_initial_baseline_does_not_swallow_the_next_upload(self):
        old, new = self.share('already-sampled'), self.share('published-after')
        baseline = self.run_grid('incremental', [old], complete=False)
        self.assertEqual((self.modes[-1], baseline['status'], self.saved), ('baseline', 'partial', []))
        self.assertIsNone(self.queue.last_complete_at(self.entry['url']))
        next_run = self.run_grid('incremental', [new, self.share('already-sampled', 'reissued')])
        self.assertEqual((self.modes[-1], next_run['status'], self.saved), ('incremental', 'done', ['published-after']))

    def test_failed_item_is_retried_and_a_partial_run_is_never_an_incremental_boundary(self):
        old, first, middle = (self.share(key) for key in ('old', 'new', 'middle'))
        self.run_grid('incremental', [old])
        self.fail.add('middle')
        partial = self.run_grid('incremental', [first, middle, old])
        self.assertEqual((partial['status'], partial['saved'], partial['failed']), ('partial', 1, 1))
        self.assertFalse(self.queue.known(self.entry['url'], 'new'))
        self.assertFalse(self.queue.known(self.entry['url'], 'middle'))
        self.fail.clear()
        result = self.run_grid('incremental', [first, middle, old])
        self.assertEqual((result['status'], result['saved'], result['skipped']), ('done', 1, 1))
        self.assertEqual(self.saved, ['new','middle','new','middle'])

    def test_history_continues_past_failed_works_and_attempts_each_fingerprint_once(self):
        urls = [self.share(key) for key in ('one', 'two', 'three', 'four')]
        self.fail.update(('one', 'two', 'three'))
        aliases = [self.share('one', 'anotherOne'), self.share('three', 'anotherThree')]
        partial = self.run_grid('history', [*urls[:3], *aliases, urls[3]])
        self.assertEqual((partial['status'], partial['saved'], partial['failed']), ('partial', 1, 3))
        self.assertEqual(self.saved, ['one', 'two', 'three', 'four'])
        self.assertIn('模拟微信列表末尾', partial['error'])
        self.assertIn('3 条作品处理失败', partial['error'])
        self.assertIn('three的视频', partial['item_error'])
        self.fail.clear()
        retry = self.run_grid('history', [aliases[0], urls[1], aliases[1], urls[3]])
        self.assertEqual((retry['status'], retry['saved'], retry['skipped']), ('done', 3, 1))

    def test_unavailable_shared_runtime_stops_before_the_next_work(self):
        unavailable = RuntimeError('无法连接微信容器')
        unavailable.status = 'runtime_unavailable'
        with patch.dict(self.saver, save=Mock(side_effect=unavailable)):
            result = self.run_grid('history', [self.share('one'), self.share('two')])
        self.assertEqual((result['status'], result['discovered'], result['failed']), ('failed', 1, 1))
        self.assertIn('无法连接微信容器', result['error'])

    def test_removed_boundary_does_not_turn_older_history_into_new_video(self):
        from datetime import datetime, timedelta, timezone
        baseline = self.run_grid('incremental', [self.share('old')])
        day = datetime.fromtimestamp(baseline['created_at'], timezone(timedelta(hours=8))).date()
        older, today = self.share('older'), self.share('today')
        self.dates = {older: (day - timedelta(days=5)).isoformat(), today: day.isoformat()}
        result = self.run_grid('incremental', [today, older])
        self.assertEqual((result['saved'], self.saved), (1, ['today']))

    def test_dashboard_cancellation_while_one_video_is_being_saved(self):
        def cancel():
            conn = HTTPConnection(*self.server.server_address)
            try:
                conn.request('POST', f'/api/crawls/{self.active_job}/cancel', '{}', {'Content-Type':'application/json'})
                response = conn.getresponse()
                self.assertEqual(json.loads(response.read())['job']['status'], 'cancelled')
            finally:
                conn.close()
        self.after_save = cancel
        result = self.run_grid('history', [self.share('one'), self.share('two')])
        self.assertEqual((result['status'], result['saved'], self.saved), ('cancelled', 1, ['one']))
        self.assertFalse(self.queue.seen_before(self.entry['url']))
        self.assertEqual(self.queue.recover(), 0)


if __name__ == '__main__':
    unittest.main()
