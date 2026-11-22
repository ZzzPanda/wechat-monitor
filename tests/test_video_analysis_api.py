"""Exercise the separate dashboard and worker HTTP endpoints on localhost."""
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'dashboard' if (root / 'dashboard/server.py').is_file()
                       else root / 'state/dashboard-app'))
from server import DashboardServer, Library
from video_analysis_queue import Queue


WORK_KEY = 'a' * 64
VIDEO = '/api/videos/' + WORK_KEY + '/analysis'
COLLECTION = '/api/video-analyses'
WORKER = '/api/video-analysis-worker'
HEADERS = {'X-Video-Analysis-Worker': 'local-fixture-video-analysis-worker'}


class VideoAnalysisApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        media = base / 'media'
        self.lib = Library(base / 'authors.txt', base / 'library.sqlite3', media / 'legacy',
                           base / 'posters', base / 'static', media)
        self.recording = media / 'custom' / '录像.mp4'
        self.recording.parent.mkdir(parents=True)
        with sqlite3.connect(self.lib.database) as db:
            db.executescript('CREATE TABLE works(fingerprint,author,caption,published);'
                             'CREATE TABLE recordings(work_key,path,size,sha256);')
            db.execute('INSERT INTO works VALUES(?,?,?,?)', (WORK_KEY, '作者', '测试视频', '2026-11-21'))
            db.execute('INSERT INTO recordings VALUES(?,?,?,?)', (WORK_KEY, './custom/录像.mp4', 0, ''))
        self.save_recording(b'test video fixture')
        self.server = DashboardServer(('127.0.0.1', 0), self.lib)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def save_recording(self, data):
        self.recording.write_bytes(data)
        self.digest = hashlib.sha256(data).hexdigest()
        with sqlite3.connect(self.lib.database) as db:
            db.execute('UPDATE recordings SET size=?, sha256=? WHERE work_key=?', (len(data), self.digest, WORK_KEY))

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None,
                               {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else None
        finally:
            connection.close()

    def enable_worker(self):
        (self.lib.authors.parent / 'video-analysis-worker.key').write_text(HEADERS['X-Video-Analysis-Worker'])

    def worker(self, op, **fields):
        status, body = self.request('POST', WORKER, {'op': op, **fields}, HEADERS)
        self.assertEqual(status, 200, body)
        return body

    def test_only_indexed_recordings_can_be_enqueued_and_use_current_digest(self):
        self.assertEqual(self.request('GET', VIDEO), (200, {'job': None, 'worker_online': False}))
        self.assertEqual(self.request('HEAD', VIDEO), (200, None))
        self.assertEqual(self.request('POST', VIDEO, {}, {'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.request('GET', VIDEO, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('POST', VIDEO, {'path': '/forged.mp4'})[0], 400)
        self.assertEqual(self.request('POST', VIDEO.replace(WORK_KEY, 'b' * 64), {})[0], 404)
        status, body = self.request('POST', VIDEO, {})
        self.assertEqual(status, 202)
        job = body['job']
        self.assertEqual((job['work_key'], job['sha256'], job['status']), (WORK_KEY, self.digest, 'queued'))
        self.assertNotIn('path', job)
        self.assertEqual(self.request('POST', VIDEO, {})[1]['job'], job)
        self.save_recording(b'different registered recording')
        self.assertIsNone(self.request('GET', VIDEO)[1]['job'])
        replacement = self.request('POST', VIDEO, {})[1]['job']
        self.assertNotEqual(replacement['id'], job['id'])
        self.assertEqual(replacement['sha256'], self.digest)
        self.recording.write_bytes(b'off-index change')
        self.assertEqual(self.request('POST', VIDEO, {})[0], 404)
        self.recording.unlink()
        self.assertEqual(self.request('GET', VIDEO)[0], 404)

    def test_worker_isolated_from_crawl_and_validates_lease(self):
        self.assertEqual(self.request('POST', WORKER, {'op': 'tick'}, HEADERS)[0], 403)
        self.enable_worker()
        crawl_key = 'local-fixture-distinct-crawl-worker'
        (self.lib.authors.parent / 'crawl-worker.key').write_text(crawl_key)
        self.assertEqual(self.request('POST', WORKER, {'op': 'tick'}, {'X-Video-Analysis-Worker': crawl_key})[0], 403)
        self.assertEqual(self.request('POST', WORKER, {'op': 'tick'})[0], 403)
        self.assertEqual(self.request('POST', '/api/crawl-worker', {'op': 'tick'}, HEADERS)[0], 403)
        self.assertEqual(self.request('GET', WORKER, headers=HEADERS)[0], 405)
        self.assertEqual(self.request('POST', WORKER, {'op': []}, HEADERS)[0], 400)
        self.assertFalse(self.request('GET', VIDEO)[1]['worker_online'])
        self.assertEqual(self.worker('claim'), {'job': None})
        self.assertTrue(self.request('GET', VIDEO)[1]['worker_online'])
        self.assertFalse(self.request('GET', '/api/crawls')[1]['worker_online'])

        enqueued = self.request('POST', VIDEO, {})[1]['job']
        claimed = self.worker('claim')['job']
        self.assertEqual((claimed['id'], claimed['path'], claimed['status']),
                         (enqueued['id'], './custom/录像.mp4', 'running'))
        lease = {'job_id': claimed['id'], 'lease_token': claimed['lease_token']}
        self.assertEqual(self.worker('heartbeat', **lease), {'ok': True})
        self.assertEqual(self.worker('update', **lease, stage='analyzing', completed=1, total=4), {'ok': True})
        self.assertEqual(self.worker('fail', **{**lease, 'lease_token': '0' * 32},
                                     error='stale', retryable=False), {'ok': False})
        active = self.request('GET', VIDEO)[1]['job']
        self.assertNotIn('lease_token', active)
        self.assertEqual((active['stage'], active['completed'], active['total']), ('analyzing', 1, 4))

        bad = {'op': 'finish', **lease, 'result': []}
        self.assertEqual(self.request('POST', WORKER, bad, HEADERS)[0], 400)
        result = {'summary': '带时间戳的摘要', 'transcript': '中文跨越普通 UI 请求上限。' * 1000}
        self.assertTrue(self.worker('finish', **lease, result=result)['ok'])
        final = self.request('GET', VIDEO)[1]['job']
        self.assertEqual((final['status'], final['result']), ('succeeded', result))
        self.assertEqual(self.worker('finish', **lease, result={}), {'ok': False})

    def test_legacy_index_still_serves_video_without_analysis(self):
        self.assertEqual(self.request('POST', VIDEO, {})[0], 202)
        with sqlite3.connect(self.lib.database) as db:
            db.execute('ALTER TABLE recordings DROP COLUMN sha256')
        self.assertEqual(self.request('GET', VIDEO)[0], 404)
        self.assertEqual(self.request('POST', VIDEO, {})[0], 404)
        self.assertEqual(self.request('HEAD', '/media/' + WORK_KEY + '.mp4')[0], 200)
        self.assertEqual(self.request('GET', COLLECTION), (200, {'jobs': [], 'worker_online': False}))

    def test_collection_is_read_only_and_same_origin(self):
        self.assertEqual(self.request('GET', COLLECTION), (200, {'jobs': [], 'worker_online': False}))
        self.assertFalse(self.server.video_analysis.path.exists())
        self.assertEqual(self.request('HEAD', COLLECTION), (200, None))
        self.assertEqual(self.request('POST', COLLECTION, {})[0], 405)
        self.assertEqual(self.request('GET', COLLECTION, headers={'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.request('HEAD', COLLECTION, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.enable_worker()
        self.worker('tick')
        self.assertEqual(self.request('GET', COLLECTION), (200, {'jobs': [], 'worker_online': True}))

    def test_collection_lists_public_status_without_deserializing_saved_results(self):
        self.enable_worker()
        self.server.video_analysis.retry_seconds = 0
        self.lib.posters.mkdir()
        (self.lib.posters / (WORK_KEY + '.jpg')).write_bytes(b'poster')
        enqueued = self.request('POST', VIDEO, {})[1]['job']

        def assert_summary(status, stage, completed=0, total=0):
            code, body = self.request('GET', COLLECTION)
            self.assertEqual(code, 200)
            summary, = body['jobs']
            self.assertEqual(summary, dict(id=enqueued['id'], work_key=WORK_KEY, title='测试视频', author='作者',
                                           poster='/posters/' + WORK_KEY + '.jpg', status=status, stage=stage,
                                           completed=completed, total=total, created_at=enqueued['created_at'],
                                           updated_at=summary['updated_at']))
            self.assertGreaterEqual(summary['updated_at'], summary['created_at'])

        def claim_lease():
            job = self.worker('claim')['job']
            return dict(job_id=job['id'], lease_token=job['lease_token'])

        assert_summary('queued', 'preparing')
        lease = claim_lease()
        self.worker('update', **lease, stage='analyzing', completed=1, total=4)
        assert_summary('running', 'analyzing', 1, 4)
        self.worker('fail', **lease, error='private upstream error', retryable=True)
        assert_summary('retry_wait', 'analyzing', 1, 4)
        self.worker('fail', **claim_lease(), error='private upstream error', retryable=False)
        assert_summary('failed', 'preparing')
        self.assertEqual(self.request('POST', VIDEO, {})[1]['job']['id'], enqueued['id'])
        lease = claim_lease()
        self.worker('update', **lease, stage='summarizing', completed=3, total=4)
        result = {'summary': '只有详情接口读取结果'}
        self.worker('finish', **lease, result=result)
        self.assertEqual(self.request('GET', VIDEO)[1]['job']['result'], result)
        with sqlite3.connect(self.server.video_analysis.path) as db:
            db.execute('UPDATE jobs SET result=? WHERE id=?', ('invalid JSON', enqueued['id']))
        assert_summary('succeeded', 'summarizing', 4, 4)

    def test_collection_hides_superseded_jobs_and_unavailable_recordings(self):
        queue = self.server.video_analysis
        previous_pipeline = Queue(queue.path, pipeline_version='previous')
        previous_pipeline.enqueue(self.lib.analysis_recording(WORK_KEY))
        self.assertEqual(self.request('GET', COLLECTION)[1]['jobs'], [])
        previous_digest = self.request('POST', VIDEO, {})[1]['job']
        other_key = 'b' * 64
        other_video = './custom/other.mp4'
        (self.lib.media_root / other_video).write_bytes(b'x')
        with sqlite3.connect(self.lib.database) as db:
            db.execute('INSERT INTO works VALUES(?,?,?,?)', (other_key, '作者', '', '2026-11-22'))
            db.execute('INSERT INTO recordings VALUES(?,?,?,?)',
                       (other_key, other_video, 1, hashlib.sha256(b'x').hexdigest()))
        other = self.request('POST', '/api/videos/' + other_key + '/analysis', {})[1]['job']
        jobs = self.request('GET', COLLECTION)[1]['jobs']
        self.assertEqual([job['id'] for job in jobs], [other['id'], previous_digest['id']])
        self.assertEqual((jobs[0]['poster'], jobs[0]['title']), (None, '未命名作品'))

        self.save_recording(b'updated video')
        self.assertEqual([job['id'] for job in self.request('GET', COLLECTION)[1]['jobs']], [other['id']])
        replaced = self.request('POST', VIDEO, {})[1]['job']
        jobs = self.request('GET', COLLECTION)[1]['jobs']
        self.assertEqual([job['id'] for job in jobs], [replaced['id'], other['id']])

        self.recording.write_bytes(b'off-index change')
        self.assertEqual([job['id'] for job in self.request('GET', COLLECTION)[1]['jobs']], [other['id']])
        (self.lib.media_root / other_video).unlink()
        self.assertEqual(self.request('GET', COLLECTION)[1]['jobs'], [])


if __name__ == '__main__':
    unittest.main()
