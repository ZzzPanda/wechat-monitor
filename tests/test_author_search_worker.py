"""Exercise the shared search queue and the recorder's exclusive player lock."""
import fcntl
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
import uuid

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'scripts'))
sys.path.insert(0, str(root/'dashboard' if (root/'dashboard').is_dir() else root/'state/dashboard-app'))
from account_search import DesktopSearch
from author_search_worker import process_queue


class SearchWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.requests = self.root/'search-requests'
        self.replies = self.root/'search-replies'
        self.requests.mkdir()
        self.replies.mkdir()
        self.lock = self.root/'save.lock'
        self.search = Mock(return_value=[])

    def submit(self, q='作者', *, ttl=30):
        job = uuid.uuid4().hex + '.json'
        (self.requests/job).write_text(json.dumps({'q': q, 'expires': time.time()+ttl}))
        return job

    def reply(self, job):
        process_queue(self.root, self.lock, self.search)
        self.assertFalse((self.requests/job).exists())
        return json.loads((self.replies/job).read_text())

    def test_search_holds_player_lock_and_delivers_results(self):
        candidates = [{'name': '真实账号', 'url': 'https://weixin.qq.com/f/test'}]
        def search(query):
            with self.lock.open('ab') as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return candidates
        self.search.side_effect = search
        self.assertEqual(self.reply(self.submit(' 账号 ')), {'results': candidates})
        self.search.assert_called_once_with('账号')

    def test_recording_owns_player(self):
        with self.lock.open('ab') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            self.assertIn('占用', self.reply(self.submit())['error'])
        self.search.assert_not_called()

    def test_invalid_and_expired_jobs_do_not_touch_wechat(self):
        self.assertIn('超时', self.reply(self.submit(ttl=-1))['error'])
        self.assertIn('格式', self.reply(self.submit('foo\nbar'))['error'])
        self.search.assert_not_called()

    def test_caller_deadline_interrupts_search_and_releases_player(self):
        def capture(query):
            try:
                time.sleep(2)
            except OSError:
                self.fail('The request deadline must bypass per-frame capture retries.')

        self.search.side_effect = capture
        self.assertIn('超时', self.reply(self.submit(ttl=.1))['error'])
        with self.lock.open('ab') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.search.assert_called_once()

    def test_interrupted_ack_and_stale_replies(self):
        job, old = self.submit(), self.replies/(uuid.uuid4().hex+'.json')
        (self.replies/job).write_text('{"results":[]}')
        old.write_text('{}')
        os.utime(old, (0, 0))
        self.assertEqual(self.reply(job), {'results': []})
        self.assertFalse(old.exists())
        self.search.assert_not_called()

    def test_worker_does_not_steal_an_active_job(self):
        job = self.submit()
        with (self.root/'search-worker.lock').open('ab') as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            process_queue(self.root, self.lock, self.search)
            self.assertTrue((self.requests/job).exists())
            self.assertFalse((self.replies/job).exists())
        self.search.assert_not_called()

    def test_dashboard_and_worker_exchange_a_result(self):
        self.search.return_value = [{'name': '搜索结果', 'url': 'https://weixin.qq.com/f/example'}]
        with ThreadPoolExecutor(max_workers=1) as ui:
            result = ui.submit(DesktopSearch(self.root, timeout=3), '查询')
            deadline = time.monotonic() + 2
            while not any(self.requests.glob('*.json')) and time.monotonic() < deadline:
                time.sleep(.01)
            process_queue(self.root, self.lock, self.search)
            self.assertEqual(result.result(timeout=3), self.search.return_value)
        self.search.assert_called_once_with('查询')
        self.assertEqual(list(self.requests.iterdir()) + list(self.replies.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
