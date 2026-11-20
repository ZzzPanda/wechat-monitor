"""Dashboard API, live author-file updates and ranged playback against an isolated index."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

root = Path(__file__).resolve().parents[1]
module = root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'
sys.path.insert(0, str(module))
from server import DashboardServer, Library


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.lib = Library(base/'authors.txt', base/'library.sqlite3', base/'media', base/'posters', base/'static')
        self.lib.static.mkdir()
        (self.lib.static/'index.html').write_text('管理页', encoding='utf-8')
        self.server = DashboardServer(('127.0.0.1', 0), self.lib)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            client.request(method, path, body=body, headers=headers or {})
            res = client.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            client.close()

    def add(self, name, headers=None):
        body = json.dumps({'name':name})
        return self.request('POST', '/api/authors', body, {'Content-Type':'application/json', **(headers or {})})

    def test_authors_are_persistent_and_deduplicated(self):
        self.assertEqual(json.loads(self.request('GET', '/api/dashboard')[2]), dict(authors=[], videos=[]))
        self.lib.authors.write_text('# 备注\n旧作者', encoding='utf-8')
        self.assertEqual(self.add(' 新作者 ')[0], 201)
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(list(pool.map(lambda n: self.add(n)[0], ('新作者','A','B'))), [200,201,201])
        self.assertTrue(self.lib.author_text().startswith('# 备注\n旧作者\n'))
        self.assertEqual(sorted(self.lib.parse_authors(self.lib.author_text())), sorted(['旧作者','新作者','A','B']))
        self.assertEqual(self.add('a\nb')[0], 400)
        self.assertEqual(self.add('#隐藏')[0], 400)
        self.assertEqual(self.add('X', {'Origin':'https://example.com'})[0], 403)
        self.assertEqual(self.add('X', {'Host':'remote.example'})[0], 403)
        self.assertEqual(self.add('X', {'Content-Type':'text/plain'})[0], 415)

    def test_history_and_ranged_video(self):
        key, bad = 'a' * 64, 'b' * 64
        self.lib.media.mkdir()
        (self.lib.media/'我.mp4').write_bytes(b'0123456789')
        self.lib.posters.mkdir()
        (self.lib.posters/(key+'.jpg')).write_bytes(b'poster')
        (self.lib.posters/(key+'.json')).write_text('{"duration":3.5}')
        with sqlite3.connect(self.lib.database) as db:
            db.executescript('CREATE TABLE works(fingerprint,author,caption,published);'
                             'CREATE TABLE recordings(work_key,path,size);')
            db.executemany('INSERT INTO works VALUES(?,?,?,?)',
                           [(key,'历史作者','标题\n介绍','2026-11-18'), (bad,'其它','其它','2026-11-17')])
            db.executemany('INSERT INTO recordings VALUES(?,?,?)',
                           [(key,'我.mp4',10), (bad,'../static/index.html',10)])
        status, _, body = self.request('GET', '/api/dashboard')
        self.assertEqual(status, 200)
        row = json.loads(body)['videos'][0]
        self.assertEqual((row['id'], row['author'], row['title'], row['duration']), (key,'历史作者','标题',3.5))
        self.assertEqual(self.request('GET', row['poster'])[2], b'poster')
        url = row['url']
        status, headers, data = self.request('GET', url, headers={'Range':'bytes=2-4'})
        self.assertEqual((status, headers['Content-Range'], data), (206,'bytes 2-4/10',b'234'))
        self.assertEqual(self.request('GET', url, headers={'Range':'bytes=-4'})[2], b'6789')
        self.assertEqual(self.request('GET', url, headers={'Range':'bytes=7-'})[2], b'789')
        self.assertEqual(self.request('GET', url, headers={'Range':'bytes=10-'})[0], 416)
        status, headers, data = self.request('HEAD', url)
        self.assertEqual((status, headers['Content-Length'], data), (200,'10',b''))
        self.assertEqual(self.request('GET', '/media/'+bad+'.mp4')[0], 404)
        self.assertEqual(self.request('GET','/%2e%2e/authors.txt')[0], 404)
        self.assertEqual(self.request('GET','/')[2].decode(), '管理页')


if __name__ == '__main__':
    unittest.main()
