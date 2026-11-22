"""Settings API persistence and compatibility with existing recording paths."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

root = Path(__file__).resolve().parents[1]
module = root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'
sys.path.insert(0, str(module))
from server import DashboardServer, Library

DIRECTORIES = '/api/settings/directories'


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.storage_root = base/'host-media'
        self.media_root = base/'mounted-media'
        self.legacy_media = self.media_root/'微信视频号'
        self.legacy_media.mkdir(parents=True)
        authors = base/'monitoring/authors.txt'
        self.settings_file = authors.with_name('settings.json')
        self.library = Library(authors, base/'library.sqlite3', self.legacy_media, base/'posters',
                               base/'static', media_root=self.media_root, storage_root=self.storage_root)
        self.server = DashboardServer(('127.0.0.1', 0), self.library, search=lambda _: [])
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path='/api/settings', data=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            client.request(method, path, body, request_headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def settings(self, method='GET', data=None):
        status, headers, body = self.request(method, data=data)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return json.loads(body)

    def browse(self, directory=None):
        query = '' if directory is None else '?' + urlencode({'path': str(directory)})
        status, headers, body = self.request('GET', DIRECTORIES + query)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return json.loads(body)

    def test_browse_storage_projects_host_paths_and_lists_directories_only(self):
        name, leaf = '归档 & 发布', '空目录'
        archive = self.media_root/name
        (archive/leaf).mkdir(parents=True)
        (archive/'sample.mp4').write_bytes(b'video')
        (self.media_root/'sample.mp4').write_bytes(b'video')
        (self.media_root/'.hidden').mkdir()
        outside = self.media_root.parent/'outside'
        outside.mkdir()
        (self.media_root/'escape').symlink_to(outside, target_is_directory=True)
        (self.media_root/'alias').symlink_to(archive, target_is_directory=True)

        projected_root = str(self.storage_root)
        listing = self.browse()
        self.assertEqual((listing['path'], listing['root'], listing['parent'], listing['can_select']),
                         (projected_root, projected_root, None, False))
        self.assertCountEqual(listing['directories'], ['微信视频号', name])
        self.assertEqual(self.browse(self.storage_root), listing)

        projected_archive = self.storage_root/name
        self.assertEqual(self.browse(projected_archive),
                         dict(path=str(projected_archive), root=projected_root, parent=projected_root,
                              directories=[leaf], can_select=True))
        self.assertEqual(self.browse(projected_archive/leaf),
                         dict(path=str(projected_archive/leaf), root=projected_root, parent=str(projected_archive),
                              directories=[], can_select=True))
        self.assertFalse(self.settings_file.exists())

    def test_browse_storage_rejects_malformed_and_unsafe_paths(self):
        (self.media_root/'alias').symlink_to(self.legacy_media, target_is_directory=True)
        for value in ('', 'relative', str(self.media_root), str(self.storage_root/'../outside'),
                      str(self.storage_root/'微信视频号/../微信视频号'),
                      str(self.storage_root/'alias'), str(self.storage_root/'alias/child'),
                      str(self.storage_root/'null\x00name')):
            with self.subTest(value=value):
                self.assertEqual(self.request('GET', DIRECTORIES + '?' + urlencode({'path': value}))[0], 400)
        bad_encoding = urlencode({'path': str(self.storage_root) + '/'}) + '%FF'
        for query in (bad_encoding, 'unknown=1', urlencode([('path', str(self.storage_root))] * 2)):
            with self.subTest(query=query):
                self.assertEqual(self.request('GET', DIRECTORIES + '?' + query)[0], 400)
        query = urlencode({'path': str(self.storage_root/'missing')})
        self.assertEqual(self.request('GET', DIRECTORIES + '?' + query)[0], 404)

    def test_browse_storage_rejects_cross_site_requests(self):
        for headers in ({'Host': 'attacker.example.test'}, {'Origin': 'https://attacker.example.test'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('GET', DIRECTORIES, headers=headers)[0], 403)

    def test_model_settings_persist_with_write_only_key(self):
        initial = self.settings()
        self.assertEqual(initial['storage_root'], str(self.storage_root))
        self.assertEqual(initial['storage_path'], str(self.storage_root/'微信视频号'))
        self.assertGreater(initial['storage_available_bytes'], 0)
        self.assertFalse(initial['model_api_key_configured'])

        key = 'fixture-model-key-do-not-expose'
        endpoint = 'https://model.example.test/v1'
        updated = self.settings('PUT', {'model_api_url': endpoint, 'model_name': 'vision-v1', 'model_api_key': key})
        for response in (updated, self.settings()):
            self.assertEqual(response['model_api_url'], endpoint)
            self.assertEqual(response['model_name'], 'vision-v1')
            self.assertTrue(response['model_api_key_configured'])
            self.assertNotIn('model_api_key', response)
            self.assertNotIn(key, json.dumps(response))
        self.assertEqual(stat.S_IMODE(self.settings_file.stat().st_mode), 0o600)
        self.assertIn(key, self.settings_file.read_text(encoding='utf-8'))

        updated = self.settings('PUT', {'model_name': 'vision-v2'})
        self.assertTrue(updated['model_api_key_configured'])
        self.assertEqual(updated['model_api_url'], endpoint)
        self.assertEqual(updated['model_name'], 'vision-v2')
        self.assertFalse(self.settings('PUT', {'model_api_key': ''})['model_api_key_configured'])
        self.assertFalse(self.settings()['model_api_key_configured'])
        self.assertNotIn(key, self.settings_file.read_text(encoding='utf-8'))

    def test_independent_partial_updates_do_not_overwrite_each_other(self):
        endpoint, name = 'https://model.example.test/v1', 'vision-v1'
        barrier = threading.Barrier(2, timeout=5)

        def update(fields):
            barrier.wait()
            return self.request('PUT', data=fields)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(update, {'model_api_url': endpoint})
            second = pool.submit(update, {'model_name': name})
            for response in (first.result(), second.result()):
                self.assertEqual(response[0], 200, response[2])
        settings = self.settings()
        self.assertEqual((settings['model_api_url'], settings['model_name']), (endpoint, name))

    def test_foreign_hosts_and_cross_site_requests_are_rejected(self):
        for headers in ({'Host': 'attacker.example.test'}, {'Origin': 'https://attacker.example.test'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            for method in ('GET', 'PUT'):
                with self.subTest(headers=headers, method=method):
                    data = {'model_name': 'foreign-model'} if method == 'PUT' else None
                    self.assertEqual(self.request(method, data=data, headers=headers)[0], 403)
        self.assertFalse(self.settings_file.exists())

    def test_invalid_storage_paths_are_rejected(self):
        outside = self.media_root.parent/'outside'
        outside.mkdir()
        (self.media_root/'escape').symlink_to(outside, target_is_directory=True)
        (self.media_root/'not-a-directory').write_text('test')
        for path in ('relative', str(self.storage_root), str(self.storage_root/'../outside'),
                     str(self.storage_root.with_name('host-media-other')/'videos'),
                     str(self.storage_root/'escape/videos'), str(self.storage_root/'not-a-directory')):
            with self.subTest(path=path):
                self.assertEqual(self.request('PUT', data={'storage_path': path})[0], 400)
        self.assertEqual(self.settings()['storage_path'], str(self.storage_root/'微信视频号'))

    def test_invalid_model_configuration_does_not_update_settings(self):
        endpoint = 'http://localhost:8081/v1'
        self.assertEqual(self.settings('PUT', {'model_api_url': endpoint})['model_api_url'], endpoint)
        for bad_url in ('file:///tmp/model', 'https://user:secret@example.test/v1',
                        'https://example.test/v1?token=secret', 'https://example.test/v1#fragment'):
            with self.subTest(url=bad_url):
                status, _, body = self.request('PUT', data={'model_api_url': bad_url, 'model_name': 'changed'})
                self.assertEqual(status, 400, body)
                self.assertNotIn(bad_url.encode(), body)
        self.assertEqual(self.request('PUT', data={'model_name': 1})[0], 400)
        settings = self.settings()
        self.assertEqual(settings['model_api_url'], endpoint)
        self.assertEqual(settings['model_name'], '')

    def test_old_and_new_recordings_play_after_multiple_storage_changes(self):
        old_key, new_key = 'a' * 64, 'b' * 64
        index_path = '作者/视频.mp4'
        old_video = self.legacy_media/index_path
        old_video.parent.mkdir()
        old_video.write_bytes(b'0123456789')
        chosen = self.storage_root/'新目录'
        self.assertEqual(self.settings('PUT', {'storage_path': str(chosen)})['storage_path'], str(chosen))
        new_video = self.media_root/'新目录'/index_path
        new_video.parent.mkdir(parents=True, exist_ok=True)
        new_video.write_bytes(b'abcdefghij')
        with sqlite3.connect(self.library.database) as db:
            db.executescript('CREATE TABLE works(fingerprint,author,caption,published);'
                             'CREATE TABLE recordings(work_key,path,size);')
            db.executemany('INSERT INTO works VALUES(?,?,?,?)', ((key, '作者', '视频', '2026-11-18')
                                                                for key in (old_key, new_key)))
            db.executemany('INSERT INTO recordings VALUES(?,?,?)',
                           ((old_key, index_path, 10), (new_key, './新目录/' + index_path, 10)))

        another = self.storage_root/'另一个目录'
        self.assertEqual(self.settings('PUT', {'storage_path': str(another)})['storage_path'], str(another))
        self.assertEqual(self.settings()['storage_path'], str(another))
        for key, expected in ((old_key, b'234'), (new_key, b'cde')):
            with self.subTest(key=key):
                status, headers, body = self.request('GET', f'/media/{key}.mp4', headers={'Range': 'bytes=2-4'})
                self.assertEqual((status, headers.get('Content-Range'), body), (206, 'bytes 2-4/10', expected))


if __name__ == '__main__':
    unittest.main()
