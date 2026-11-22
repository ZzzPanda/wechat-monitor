"""Exercise the durable analysis queue without a live library or worker."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'))
from video_analysis_queue import LeaseError, Queue, public_job


class VideoAnalysisQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'video-analysis.sqlite3'
        self.queue = Queue(self.path, lease_seconds=10, max_attempts=2, retry_seconds=5)
        self.recording = dict(work_key='a'*64, path='作者/样本.mp4', sha256='b'*64, size=1024)

    def test_enqueue_is_independent_and_idempotent_across_restarts(self):
        key, digest = (self.recording[field] for field in ('work_key', 'sha256'))
        self.assertIsNone(self.queue.get(key, digest))
        self.assertFalse(self.path.exists())
        job = self.queue.enqueue(self.recording)
        self.assertEqual((job['status'], job['stage']), ('queued', 'preparing'))
        reopened = Queue(self.path)
        self.assertEqual(reopened.enqueue(self.recording), job)
        self.assertEqual(reopened.get(key, digest), job)
        for new_job in (reopened.enqueue(dict(self.recording, sha256='c'*64)),
                        Queue(self.path, pipeline_version='2').enqueue(self.recording)):
            self.assertNotEqual(new_job['id'], job['id'])

    def test_concurrent_submission_and_claim_make_one_job_and_lease(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(lambda _: self.queue.enqueue(self.recording)['id'], range(10)))
            claims = [job for job in pool.map(lambda _: self.queue.claim(), range(10)) if job is not None]
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['id'], ids[0])
        self.assertTrue(claims[0]['lease_token'])
        self.assertNotIn('path', public_job(claims[0]))
        self.assertNotIn('lease_token', public_job(claims[0]))
        self.assertNotIn('lease_token', self.queue.get(self.recording['work_key'], self.recording['sha256']))

    def test_heartbeat_renews_lease_and_progress_and_result_are_persistent(self):
        with patch('video_analysis_queue.time.time', return_value=100):
            job_id = self.queue.enqueue(self.recording)['id']
            token = self.queue.claim()['lease_token']
        with patch('video_analysis_queue.time.time', return_value=109):
            self.queue.heartbeat(job_id, token)
        with patch('video_analysis_queue.time.time', return_value=112):
            self.assertIsNone(self.queue.claim())
            progress = self.queue.update(job_id, token, 'analyzing', 1, 3)
            self.assertEqual((progress['stage'], progress['completed'], progress['total']), ('analyzing', 1, 3))
        result = {'summary': '示例摘要', 'chapters': [{'start': 2.5}]}
        with patch('video_analysis_queue.time.time', return_value=120):
            self.assertEqual(self.queue.finish(job_id, token, result)['result'], result)
        saved = Queue(self.path).get(self.recording['work_key'], self.recording['sha256'])
        self.assertEqual((saved['result'], saved['status'], saved['completed']), (result, 'succeeded', 3))
        self.assertEqual(self.queue.enqueue(self.recording), saved)

    def test_expired_lease_recovers_on_restart_and_old_token_cannot_write(self):
        with patch('video_analysis_queue.time.time', return_value=100):
            job_id = self.queue.enqueue(self.recording)['id']
            stale = self.queue.claim()['lease_token']
        queue = Queue(self.path, lease_seconds=10, max_attempts=2)
        with patch('video_analysis_queue.time.time', return_value=110):
            with self.assertRaises(LeaseError):
                queue.finish(job_id, stale, {'summary': '过期'})
            claimed = queue.claim()
            self.assertEqual(claimed['id'], job_id)
            self.assertNotEqual(claimed['lease_token'], stale)
            with self.assertRaises(LeaseError):
                queue.update(job_id, stale, 'analyzing', 1, 2)
        with patch('video_analysis_queue.time.time', return_value=120):
            self.assertIsNone(queue.claim())
            self.assertEqual(queue.get(self.recording['work_key'], self.recording['sha256'])['status'], 'failed')

    def test_transient_failures_back_off_then_allow_manual_retry(self):
        with patch('video_analysis_queue.time.time', return_value=100):
            job_id = self.queue.enqueue(self.recording)['id']
            token = self.queue.claim()['lease_token']
            failed = self.queue.fail(job_id, token, '服务暂时限流', True)
            self.assertEqual((failed['status'], failed['available_at']), ('retry_wait', 105))
            self.assertIsNone(self.queue.claim())
            moved = dict(self.recording, path='./替代媒体目录/样本.mp4')
            self.assertEqual(self.queue.enqueue(moved)['status'], 'retry_wait')
        with patch('video_analysis_queue.time.time', return_value=105):
            claim = self.queue.claim()
            self.assertEqual((claim['id'], claim['path']), (job_id, moved['path']))
            self.assertEqual(self.queue.fail(job_id, claim['lease_token'], '仍在限流', True)['status'], 'failed')
            self.assertIsNone(self.queue.claim())
            restarted = self.queue.enqueue(moved)
            self.assertEqual((restarted['id'], restarted['status'], restarted['attempts'], restarted['error']),
                             (job_id, 'queued', 0, ''))

    def test_permanent_failure_and_invalid_progress(self):
        job_id = self.queue.enqueue(self.recording)['id']
        token = self.queue.claim()['lease_token']
        with self.assertRaises(ValueError):
            self.queue.update(job_id, token, 'analyzing', 2, 1)
        for result in (['not an object'], {'numeric': float('nan')}, {'summary': '\ud800'}):
            with self.assertRaises(ValueError):
                self.queue.finish(job_id, token, result)
        self.assertEqual(self.queue.fail(job_id, token, '文件内容不匹配', False)['status'], 'failed')
        self.assertIsNone(self.queue.claim())


if __name__ == '__main__':
    unittest.main()
