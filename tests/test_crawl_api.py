"""Exercise the dashboard and worker HTTP entry points without opening WeChat."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'))
from server import DashboardServer, Library


class CrawlApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.lib = Library(base/'authors.txt', base/'library.sqlite3', base/'media', base/'posters', base/'static')
        self.lib.authors.write_text('待关联作者\n', encoding='utf-8')
        self.lib.add_author(dict(name='已关联作者', url='https://weixin.qq.com/f/fixture'))
        self.server = DashboardServer(('127.0.0.1', 0), self.lib)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.author = self.lib.entries()[-1]
        self.payload = dict(name=self.author['name'], version=self.author['version'])

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None,
                               {'Content-Type':'application/json', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_only_current_verified_author_can_enqueue_and_cancel_from_dashboard(self):
        status, data = self.request('GET', '/api/crawls')
        self.assertEqual((status, data['worker_online'], data['jobs']), (200, False, []))
        post = lambda body, **headers: self.request('POST', '/api/crawls', body, headers)
        self.assertEqual(post(self.payload, Origin='https://other.example')[0], 403)
        self.assertEqual(post({**self.payload, 'version':'stale'})[0], 409)
        self.assertEqual(post({**self.payload, 'name':'不存在'})[0], 404)
        self.assertEqual(post(dict(name='待关联作者', version=self.lib.entries()[0]['version']))[0], 400)
        self.assertEqual(post({**self.payload, 'profile_url':'forged'})[0], 400)
        status, data = post(self.payload)
        job = data['job']
        self.assertEqual((status, job['status'], job['mode']), (202, 'queued', 'history'))
        self.assertEqual(post(self.payload)[1]['job']['id'], job['id'])
        path = '/api/crawls/'+job['id']+'/cancel'
        self.assertEqual(self.request('POST', path, {}, {'Sec-Fetch-Site':'cross-site'})[0], 403)
        self.assertEqual(self.request('POST', path, {'id':'wrong'})[0], 400)
        for _ in range(2):
            self.assertEqual(self.request('POST', path, {})[1]['job']['status'], 'cancelled')
        self.assertEqual(self.request('GET', '/api/crawls')[1]['jobs'][0]['status'], 'cancelled')

    def test_schedule_validation_and_public_status(self):
        path = '/api/crawl-settings'
        data = dict(enabled=False, interval_minutes=43200, start_at=2000000000)
        self.assertEqual(self.request('PATCH', path, data, {'Origin':'http://other.example'})[0], 403)
        for invalid in ({'enabled':True}, {**data,'interval_minutes':59}, {**data,'enabled':'true'},
                        {**data,'start_at':None}, {**data,'interval_minutes':10**60}, {**data, 'extra':1}):
            self.assertEqual(self.request('PATCH', path, invalid)[0], 400)
        status, body = self.request('PATCH', path, data)
        self.assertEqual(status, 200)
        self.assertFalse(body['settings']['enabled'])
        self.assertEqual(body['settings']['interval_minutes'], 43200)
        self.assertEqual(body['settings']['start_at'], data['start_at'])
        self.assertEqual(self.request('GET', '/api/crawls')[1]['settings'], body['settings'])
        status, old_client = self.request('PATCH', path, dict(enabled=True, interval_minutes=60))
        self.assertEqual(status, 200)
        self.assertEqual(old_client['settings']['start_at'], data['start_at'])

    def test_worker_credential_and_durable_progress(self):
        path = '/api/crawl-worker'
        credential = {'X-Crawl-Worker':'dummy-test-secret-for-isolated-local-server'}
        self.assertEqual(self.request('POST', path, {'op':'tick'}, credential)[0], 403)
        (self.lib.authors.parent/'crawl-worker.key').write_text(credential['X-Crawl-Worker'])
        self.assertEqual(self.request('POST', path, {'op':'tick'})[0], 403)
        self.assertEqual(self.request('POST', path, {'op':'tick'}, {'X-Crawl-Worker':'wrong'})[0], 403)

        def worker(op, **fields):
            status, result = self.request('POST', path, dict(op=op, **fields), credential)
            self.assertEqual(status, 200, result)
            return result

        _, reply = self.request('POST', '/api/crawls', self.payload)
        job_id = reply['job']['id']
        self.assertTrue(worker('tick')['queued'])
        self.assertTrue(self.request('GET','/api/crawls')[1]['worker_online'])
        self.assertEqual(worker('claim')['job']['id'], job_id)
        self.assertTrue(worker('active', id=job_id, author=self.author['name'], profile_url=self.author['url'])['active'])
        self.assertFalse(worker('known', profile_url=self.author['url'], key='key')['known'])
        self.assertEqual(worker('record', id=job_id, url='share', key='key')['item']['status'], 'pending')
        worker('item_done', id=job_id, url='share', status='saved')
        worker('finish', id=job_id, status='done')
        self.assertTrue(worker('known', profile_url=self.author['url'], key='key')['known'])
        self.assertTrue(worker('seen_before', profile_url=self.author['url'])['known'])
        saved = self.request('GET', '/api/crawls')[1]['jobs'][0]
        self.assertEqual((saved['status'], saved['discovered'], saved['saved']), ('done', 1, 1))


if __name__ == '__main__':
    unittest.main()
