"""The dashboard owns crawl state. Use isolated files and no live WeChat session."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'))
from crawl_queue import Queue


class CrawlQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'crawl.sqlite3'
        self.queue = Queue(self.path)
        self.author = dict(name='示例作者', url='https://weixin.qq.com/f/demo')

    def start(self, mode='history'):
        job = self.queue.enqueue(self.author, mode)
        self.assertEqual(self.queue.claim([self.author])['id'], job['id'])
        return job['id']

    def test_schedule_respects_interval_disable_and_legacy_accounts(self):
        with patch('crawl_queue.time.time', return_value=100):
            self.assertEqual(self.queue.snapshot(), dict(jobs=[], settings=dict(enabled=True, interval_minutes=60, next_run=3700, start_at=3700)))
            config = self.queue.configure(True, 60, start_at=400)
            entries = [dict(name='旧名字', url=None), self.author]
            self.assertEqual(config['next_run'], 400)
            self.assertEqual(self.queue.schedule(entries, now=399), [])
            ids = self.queue.schedule(entries, now=400)
            self.assertEqual(len(ids), 2)
            self.assertEqual(self.queue.schedule(entries, now=401), [])
            jobs = {row['author']: row for row in self.queue.snapshot()['jobs']}
            self.assertEqual(jobs['旧名字']['status'], 'failed')
            self.assertIn('主页', jobs['旧名字']['error'])
            active = self.queue.claim(entries)
            self.assertEqual((active['author'], active['mode']), ('示例作者', 'incremental'))
            self.assertIn(active['id'], self.queue.schedule(entries, now=4000))
            self.queue.configure(False, 60)
            self.assertEqual(self.queue.schedule(entries, now=8500), [])
            for value in (True, 59, 10**50):
                with self.assertRaises(ValueError):
                    self.queue.configure(True, value)
            monthly = self.queue.configure(True, 30 * 24 * 60)
            self.assertEqual(monthly['interval_minutes'], 43200)

    def test_legacy_job_reports_unverified_cards_without_exposing_raw_error(self):
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE jobs(id TEXT PRIMARY KEY, author TEXT, profile_url TEXT,
                       mode TEXT, status TEXT, error TEXT, created_at INTEGER, updated_at INTEGER)''')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                       ('legacy', '示例', 'profile', 'incremental', 'partial',
                        '已到历史边界；2 张作品卡未能核验分享链接。最近原因：private detail', 1, 2))
        self.assertEqual(self.queue.snapshot()['jobs'][0]['unverified_cards'], 2)
        message = self.queue.log_entries(1, 2, 1)[0]['message']
        self.assertIn('2 张作品卡未能核验', message)
        self.assertNotIn('private', message)

    def test_finish_retry_preserves_partial_result_from_unverified_cards(self):
        job = self.start('incremental')
        first = self.queue.finish(job, 'done', unverified_cards=2)
        self.assertEqual((first['status'], first['unverified_cards']), ('partial', 2))
        self.assertEqual(self.queue.finish(job, 'done', unverified_cards=2), first)

    def test_fixed_start_keeps_daily_slots_after_sleep_and_restart(self):
        day = 86400
        anchor = 100000
        with patch('crawl_queue.time.time', return_value=anchor - 200):
            config = self.queue.configure(True, 1440, start_at=anchor)
            self.assertEqual((config['start_at'], config['next_run']), (anchor, anchor))
        entries = [self.author]
        self.assertEqual(self.queue.schedule(entries, now=anchor - 1), [])
        self.assertEqual(len(self.queue.schedule(entries, now=anchor)), 1)
        self.assertEqual(self.queue.snapshot()['settings']['next_run'], anchor + day)
        with patch('crawl_queue.time.time', return_value=anchor):
            self.assertEqual(self.queue.claim(entries)['mode'], 'incremental')
        first = self.queue.snapshot()['jobs'][0]['id']
        self.queue.finish(first, 'done')
        self.queue = Queue(self.path)
        wake_up = anchor + day * 4 + 1234
        self.assertEqual(len(self.queue.schedule(entries, now=wake_up)), 1)
        self.assertEqual(self.queue.schedule(entries, now=wake_up + 1), [])
        self.assertEqual(len(self.queue.snapshot()['jobs']), 2)
        self.assertEqual(self.queue.snapshot()['settings']['next_run'], anchor + day * 5)
        with patch('crawl_queue.time.time', return_value=wake_up):
            config = self.queue.configure(False, 120)
            self.assertEqual((config['start_at'], config['next_run']), (anchor, anchor + day * 4 + 7200))
            with self.assertRaises(ValueError):
                self.queue.configure(True, 60, start_at=True)

    def test_legacy_schedule_migrates_next_run_and_interval_without_losing_disabled_state(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE settings(singleton INTEGER PRIMARY KEY,enabled,interval_minutes,next_run)')
            conn.execute('INSERT INTO settings VALUES(1,0,5,123456)')
        settings = self.queue.snapshot()['settings']
        self.assertEqual(settings, dict(enabled=False, interval_minutes=60, start_at=123456, next_run=123456))
        self.assertEqual(Queue(self.path).snapshot()['settings'], settings)

    def test_concurrent_enqueue_and_claim_recheck_current_list(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(lambda _: self.queue.enqueue(self.author)['id'], range(10)))
        self.assertEqual(len(set(ids)), 1)
        self.assertTrue(self.queue.has_queued())
        self.assertIsNone(self.queue.claim([{**self.author, 'url': 'another-profile'}]))
        self.assertEqual(self.queue.snapshot()['jobs'][0]['status'], 'cancelled')
        self.assertFalse(self.queue.has_queued())
        self.queue.enqueue(self.author)
        self.assertIsNone(self.queue.claim([]))

    def test_boundary_yields_after_item_then_drains_whole_incremental_round_and_resumes(self):
        peer = dict(name='另一个作者', url='https://weixin.qq.com/f/other')
        entries = [self.author, peer]
        with patch('crawl_queue.time.time', return_value=100):
            self.queue.configure(True, 60, start_at=200)
            job = self.start()
            self.queue.record(job, 'one', 'key1')
            self.queue.item_done(job, 'one', 'saved')
            self.queue.record(job, 'two', 'key2')
            self.queue.item_done(job, 'two', 'failed', '无法打开')
            self.assertFalse(self.queue.yield_history(job, {'next': 1}))
        cursor = {'page': 7, 'next': 2, 'signature': '页内定位'}
        def boundary():
            return self.queue.activity(job, self.author['name'], self.author['url'], entries, boundary=True)
        with patch('crawl_queue.time.time', return_value=200), ThreadPoolExecutor(max_workers=3) as pool:
            at_boundary = pool.submit(boundary)
            heartbeat = pool.submit(self.queue.schedule, entries)
            at_claim = pool.submit(self.queue.claim, entries)
            self.assertEqual(at_boundary.result(), dict(active=True, yield_requested=True))
            heartbeat.result()
            self.assertIsNone(at_claim.result())
        self.assertFalse(self.queue.activity(job, peer['name'], peer['url'], entries)['active'])
        self.assertTrue(self.queue.yield_history(job, cursor))
        self.assertFalse(self.queue.yield_history(job, {'next': 42}))
        self.assertEqual(self.queue.enqueue(self.author)['id'], job)
        before = {row['id']: row for row in self.queue.snapshot()['jobs']}
        self.assertEqual(len(before), 3)
        self.assertEqual((before[job]['status'], before[job]['saved'], before[job]['failed']), ('queued', 1, 1))
        self.assertIn('增量', before[job]['error'])
        self.assertNotIn('checkpoint', before[job])
        self.queue = Queue(self.path)
        clock = patch('crawl_queue.time.time', return_value=300)
        clock.start()
        self.addCleanup(clock.stop)
        for author in entries:
            first = self.queue.claim(entries)
            self.assertEqual((first['mode'], first['author']), ('incremental', author['name']))
            self.assertFalse(self.queue.yield_history(first['id'], cursor))
            self.queue.finish(first['id'], 'done')
        latest = self.queue.claim(entries)
        self.assertEqual((latest['id'], latest['checkpoint'], latest['error']), (job, cursor, ''))
        self.assertEqual(self.queue.recover(), 1)
        self.assertEqual(self.queue.claim(entries)['checkpoint'], cursor)
        result = self.queue.finish(job, 'done', '列表到底')
        self.assertEqual((result['status'], result['saved'], result['failed']), ('partial', 1, 1))
        self.assertIn('1 条作品处理失败', result['error'])
        self.assertFalse(self.queue.yield_history(job, {}))
        with self.queue.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM history_checkpoints').fetchone())

    def test_claim_enqueues_due_incremental_without_heartbeat(self):
        with patch('crawl_queue.time.time', return_value=100):
            self.queue.configure(True, 60, start_at=200)
            job = self.queue.enqueue(self.author)
        with patch('crawl_queue.time.time', return_value=200):
            claimed = self.queue.claim([self.author])
            self.assertEqual(claimed['mode'], 'incremental')
            self.assertFalse(self.queue.activity(claimed['id'], self.author['name'], self.author['url'],
                                                [self.author], boundary=True)['yield_requested'])
            self.queue.finish(claimed['id'], 'done')
            history = self.queue.claim([self.author])
            self.assertEqual((history['id'], history['checkpoint']), (job['id'], None))

    def test_withdrawn_incremental_and_cancelled_history_cannot_be_yielded(self):
        job = self.start()
        inc = self.queue.enqueue(self.author, 'incremental')
        self.assertTrue(self.queue.activity(job, self.author['name'], self.author['url'], [self.author])['yield_requested'])
        self.queue.cancel(inc['id'])
        self.assertFalse(self.queue.yield_history(job, {}))
        inc = self.queue.enqueue(self.author, 'incremental')
        self.queue.cancel(job)
        self.assertFalse(self.queue.yield_history(job, {}))
        self.assertEqual(self.queue.claim([self.author])['id'], inc['id'])
        self.queue.finish(inc['id'], 'done')
        self.assertIsNone(self.queue.claim([self.author]))

    def test_recovery_preserves_item_identity_and_retries_failures(self):
        job = self.start()
        self.queue.record(job, 'share1', 'one')
        self.queue.item_done(job, 'share1', 'saved')
        self.queue.record(job, 'share2', 'two')
        self.queue.item_done(job, 'share2', 'failed', '获取失败')
        self.queue = Queue(self.path)
        self.assertEqual(self.queue.recover(), 1)
        self.assertEqual(self.queue.claim([self.author])['id'], job)
        self.assertEqual(self.queue.record(job, 'share1-again', 'one'), dict(key='one', url='share1', status='saved'))
        self.queue.item_done(job, 'share1-again', 'skipped')
        self.assertEqual(self.queue.record(job, 'share2-again', 'two')['status'], 'failed')
        self.queue.item_done(job, 'share2-again', 'saved')
        with self.assertRaises(ValueError):
            self.queue.record(job, 'share2-again', 'unexpected')
        result = self.queue.finish(job, 'done')
        self.assertEqual((result['discovered'], result['saved'], result['failed']), (2, 2, 0))
        self.assertTrue(self.queue.known(self.author['url'], 'two'))
        self.assertTrue(self.queue.seen_before(self.author['url']))
        self.assertFalse(self.queue.known('another-profile', 'two'))

    def test_only_completed_runs_set_incremental_boundaries(self):
        job = self.start('incremental')
        self.queue.record(job, 'old', 'base')
        self.queue.item_done(job, 'old', 'baseline')
        self.assertFalse(self.queue.known(self.author['url'], 'base'))
        result = self.queue.finish(job, 'done')
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(self.queue.known(self.author['url'], 'base'))
        job = self.start('incremental')
        self.queue.record(job, 'new', 'latest')
        self.queue.item_done(job, 'new', 'saved')
        self.queue.record(job, 'failed', 'middle')
        self.queue.item_done(job, 'failed', 'failed')
        self.assertEqual(self.queue.finish(job, 'done')['status'], 'partial')
        self.assertFalse(self.queue.known(self.author['url'], 'latest'))
        self.assertFalse(self.queue.known(self.author['url'], 'middle'))
        self.assertTrue(self.queue.known(self.author['url'], 'base'))

    def test_publication_cutoff_uses_start_of_last_fully_completed_scan_for_this_profile(self):
        url = self.author['url']
        self.assertIsNone(self.queue.last_complete_at(url))
        with patch('crawl_queue.time.time', return_value=100):
            first = self.start('incremental')
        with patch('crawl_queue.time.time', return_value=500):
            self.queue.finish(first, 'done')
        self.assertEqual(self.queue.last_complete_at(url), 100)
        with patch('crawl_queue.time.time', return_value=700):
            for status in ('partial', 'failed', 'cancelled'):
                job = self.start('incremental')
                self.queue.finish(job, status)
            current = self.start('history')
        self.assertEqual(self.queue.last_complete_at(url), 100)
        with patch('crawl_queue.time.time', return_value=900):
            self.queue.finish(current, 'done')
        self.assertEqual(Queue(self.path).last_complete_at(url), 700)
        self.assertIsNone(self.queue.last_complete_at('different-profile'))

    def test_only_verified_items_from_first_partial_baseline_count_as_known(self):
        url = self.author['url']
        with patch('crawl_queue.time.time', return_value=100):
            job = self.start('incremental')
        for state in ('baseline', 'saved', 'skipped', 'failed'):
            self.queue.record(job, state, state)
            self.queue.item_done(job, state, state)
        self.assertIsNone(self.queue.initial_baseline_at(url))
        self.queue.finish(job, 'partial')
        self.assertTrue(self.queue.seen_before(url))
        self.assertIsNone(self.queue.last_complete_at(url))
        self.assertEqual(self.queue.initial_baseline_at(url), 100)
        self.assertTrue(self.queue.known(url, 'baseline'))
        for key in ('saved', 'skipped', 'failed'):
            self.assertFalse(self.queue.known(url, key))
        later = self.start('incremental')
        self.queue.record(later, 'legacy-extra', 'new-upload')
        self.queue.item_done(later, 'legacy-extra', 'baseline')
        self.queue.finish(later, 'partial')
        self.assertEqual(self.queue.initial_baseline_at(url), 100)
        self.assertFalse(self.queue.known(url, 'new-upload'))
        self.assertIsNone(self.queue.initial_baseline_at('another-profile'))

    def test_resolving_a_failed_preview_merges_into_a_known_work_in_the_same_job(self):
        job = self.start()
        placeholder = 'unresolved:' + hashlib.sha256(b'link').hexdigest()
        self.queue.record(job, 'link', placeholder)
        self.queue.item_done(job, 'link', 'failed', '读取预览失败')
        self.queue.record(job, 'other-link', 'stable-key')
        self.queue.item_done(job, 'other-link', 'saved')
        self.assertEqual(self.queue.record(job, 'link', 'stable-key')['status'], 'saved')
        self.queue.item_done(job, 'link', 'skipped')
        result = self.queue.finish(job, 'done')
        self.assertEqual((result['discovered'], result['saved'], result['failed']), (1, 1, 0))

    def test_cancelling_does_not_promote_running_or_inflight_saves(self):
        job = self.start()
        self.assertTrue(self.queue.is_running(job))
        self.queue.record(job, 'inflight', 'existing')
        self.assertEqual(self.queue.cancel(job)['status'], 'cancelled')
        self.assertFalse(self.queue.is_running(job))
        with self.assertRaises(ValueError):
            self.queue.record(job, 'later', 'next')
        self.queue.item_done(job, 'inflight', 'saved')
        result = self.queue.finish(job, 'done')
        self.assertEqual((result['status'], result['saved']), ('cancelled', 1))
        self.assertFalse(self.queue.seen_before(self.author['url']))
        self.assertFalse(self.queue.known(self.author['url'], 'existing'))
        self.assertEqual(self.queue.recover(), 0)
        with self.assertRaises(KeyError):
            self.queue.cancel('missing')

    def test_history_shows_last_fifty_without_losing_older_deduplication(self):
        first = self.start()
        self.queue.record(first, 'seed', 'key')
        self.queue.item_done(first, 'seed', 'saved')
        self.queue.finish(first, 'done')
        for index in range(51):
            self.queue.enqueue(dict(name=f'旧显示名{index}', url=None))
        jobs = self.queue.snapshot()['jobs']
        self.assertEqual(len(jobs), 50)
        self.assertEqual(jobs[0]['author'], '旧显示名50')
        self.assertTrue(self.queue.known(self.author['url'], 'key'))

    def test_old_yielded_and_running_jobs_stay_visible_ahead_of_last_fifty_results(self):
        history = self.start()
        incremental = self.queue.enqueue(self.author, 'incremental')['id']
        self.assertTrue(self.queue.yield_history(history, {'next': 3}))
        self.assertEqual(self.queue.claim([self.author])['id'], incremental)
        for index in range(51):
            self.queue.enqueue(dict(name=f'未关联作者{index}', url=None))
        jobs = self.queue.snapshot()['jobs']
        self.assertEqual(len(jobs), 52)
        self.assertEqual((jobs[0]['id'], jobs[0]['status']), (incremental, 'running'))
        self.assertEqual((jobs[1]['id'], jobs[1]['status']), (history, 'queued'))
        self.assertEqual((jobs[2]['author'], jobs[-1]['author']), ('未关联作者50', '未关联作者1'))


if __name__ == '__main__':
    unittest.main()
