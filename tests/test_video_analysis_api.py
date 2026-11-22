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


WORK_KEY = 'a' * 64
VIDEO = '/api/videos/' + WORK_KEY + '/analysis'
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
        with sqlite3.connect(self.lib.database) as db:
            db.execute('ALTER TABLE recordings DROP COLUMN sha256')
        self.assertEqual(self.request('GET', VIDEO)[0], 404)
        self.assertEqual(self.request('POST', VIDEO, {})[0], 404)
        self.assertEqual(self.request('HEAD', '/media/' + WORK_KEY + '.mp4')[0], 200)


if __name__ == '__main__':
    unittest.main()
