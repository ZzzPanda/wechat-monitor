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
from unittest.mock import patch
from urllib.parse import quote, urlencode

root = Path(__file__).resolve().parents[1]
module = root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'
sys.path.insert(0, str(module))
from server import DashboardServer, Library


def profile(name, identity):
    return {'name': name, 'url': 'https://weixin.qq.com/f/' + identity, 'id': identity}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.lib = Library(base/'authors.txt', base/'library.sqlite3', base/'media', base/'posters', base/'static')
        self.lib.static.mkdir()
        (self.lib.static/'index.html').write_text('管理页', encoding='utf-8')
        self.matches = {}
        self.queries = []

        def search(query):
            self.queries.append(query)
            return self.matches.get(query, [])

        self.server = DashboardServer(('127.0.0.1', 0), self.lib, search=search)
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

    def submit(self, data, headers=None):
        return self.request('POST', '/api/authors', json.dumps(data),
                            {'Content-Type': 'application/json', **(headers or {})})

    def search_response(self, query, *, fresh=False):
        params = {'q': query}
        if fresh:
            params['fresh'] = '1'
        status, _, body = self.request('GET', '/api/author-search?' + urlencode(params))
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def search(self, query):
        return self.search_response(query)['results']

    def saved_profiles(self):
        status, _, body = self.request('GET', '/api/dashboard')
        self.assertEqual(status, 200)
        return [(p['name'], p['url']) for p in json.loads(body)['profiles']]

    def entries(self):
        status, _, body = self.request('GET', '/api/authors')
        self.assertEqual(status, 200, body)
        return json.loads(body)['entries']

    def change(self, method, name, data, headers=None):
        path = '/api/authors/' + quote(name, safe='')
        status, _, body = self.request(method, path, json.dumps(data),
                                       {'Content-Type': 'application/json', **(headers or {})})
        return status, json.loads(body)

    def test_plain_text_and_unselected_links_cannot_add_an_author(self):
        candidate = profile('新作者', 'fixtureAccount1')
        channels = 'https://channels.weixin.qq.com/web/pages/'
        self.matches['作者'] = [
            candidate, candidate, profile('#隐藏', 'fixtureAccount2'), profile('分\n行', 'fixtureAccount3'),
            {'name': '文章', 'url': 'https://mp.weixin.qq.com/s/fixture'},
            {'name': '非微信链接', 'url': 'https://example.com/f/fixture'},
            {'name': '其它页面', 'url': channels + 'video?username=fixture'},
            {'name': '多余参数', 'url': channels + 'profile?username=fixture&extra=1'},
            {'name': '重复账号', 'url': channels + 'profile?username=fixture&username=other'},
        ]
        self.assertEqual(self.search('无结果'), [])
        result, = self.search('作者')
        self.assertEqual(self.queries, ['无结果', '作者'])
        self.assertEqual((result['name'], result['url']), (candidate['name'], candidate['url']))
        self.assertTrue(result['token'])
        for data in ({'name': '任意名称'}, {'url': candidate['url']},
                     {'token': result['token'], 'name': '替换名称'}, {'token': candidate['url']},
                     {'token': '无效凭证'}, {}):
            with self.subTest(data=data):
                self.assertEqual(self.submit(data)[0], 400)
        self.assertEqual(self.lib.author_text(), '')
        self.assertEqual(self.saved_profiles(), [])

    def test_expired_selection_requires_a_new_search(self):
        self.matches['作者'] = [profile('作者', 'fixtureAccount')]
        with patch('account_search.time') as clock:
            clock.monotonic.return_value = 100
            old, = self.search('作者')
            clock.monotonic.return_value += 601
            self.assertEqual(self.submit({'token': old['token']})[0], 400)
            self.assertEqual(self.lib.author_text(), '')
            fresh, = self.search('作者')
            self.assertEqual(self.submit({'token': fresh['token']})[0], 201)
            self.assertEqual(self.queries, ['作者', '作者'])

    def test_cache_matches_only_the_same_keyword_and_issues_new_tokens(self):
        candidate = profile('目标账号', 'fixtureTarget')
        self.matches['目标'] = [candidate]
        self.matches['目标账号'] = [profile('另一个账号', 'fixtureOther')]
        original = self.search_response('目标')
        cached = self.search_response('目标')
        self.assertFalse(original['cached'])
        self.assertTrue(cached['cached'])
        first, = original['results']
        again, = cached['results']
        self.assertEqual(again['url'], first['url'])
        self.assertNotEqual(again['token'], first['token'])
        self.assertEqual(self.submit({'token': again['url']})[0], 400)
        self.assertEqual(self.submit({'token': again['token']})[0], 201)
        other = self.search_response('目标账号')
        self.assertFalse(other['cached'])
        self.assertEqual(other['results'][0]['name'], '另一个账号')
        self.assertEqual(self.queries, ['目标', '目标账号'])

    def test_fresh_search_replaces_cached_matches_and_clears_them_on_empty(self):
        self.matches['目标'] = [profile('旧结果', 'fixtureOld')]
        self.search('目标')
        self.matches['目标'] = [profile('新结果', 'fixtureNew')]
        updated = self.search_response('目标', fresh=True)
        self.assertFalse(updated['cached'])
        self.assertEqual(updated['results'][0]['name'], '新结果')
        cached = self.search_response('目标')
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['results'][0]['name'], '新结果')
        self.matches['目标'] = []
        self.assertEqual(self.search_response('目标', fresh=True), {'results': [], 'cached': False})
        self.assertEqual(self.search_response('目标'), {'results': [], 'cached': False})
        self.assertEqual(self.queries, ['目标'] * 4)

    def test_unverified_results_are_retried(self):
        self.matches['作者'] = [{'name': '作者', 'url': 'https://example.com/fixture'}]
        path = '/api/author-search?' + urlencode({'q': '作者'})
        self.assertEqual(self.request('GET', path)[0], 503)
        self.matches['作者'] = [profile('作者', 'fixtureValid')]
        self.assertFalse(self.search_response('作者')['cached'])
        self.assertEqual(self.queries, ['作者', '作者'])

    def test_selected_identity_controls_same_name_results(self):
        first = profile('同名作者', 'fixtureAccount1')
        second = {'name': '同名作者', 'id': 'fixtureAccount2',
                  'url': 'https://channels.weixin.qq.com/web/pages/profile?username=fixtureAccount2%40finder'}
        self.matches['同名'] = [first, second]
        results = {item['url']: item['token'] for item in self.search('同名')}
        self.assertEqual(set(results), {first['url'], second['url']})
        self.assertNotEqual(results[first['url']], results[second['url']])
        self.assertEqual(self.submit({'token': results[second['url']]})[0], 201)
        self.assertEqual(self.submit({'token': results[first['url']]})[0], 409)
        self.assertEqual(self.saved_profiles(), [(second['name'], second['url'])])
        self.assertEqual(self.lib.parse_authors(self.lib.author_text()), ['同名作者'])

    def test_existing_plain_name_can_be_linked_without_a_duplicate(self):
        self.lib.authors.write_text('# 手工配置\n旧作者', encoding='utf-8')
        candidate = profile('旧作者', 'fixtureLegacyAccount')
        self.matches['旧作者'] = [candidate]
        self.assertEqual(self.saved_profiles(), [])
        result, = self.search('旧作者')
        status, _, body = self.submit({'token': result['token']})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['added'])
        self.assertEqual(self.saved_profiles(), [('旧作者', candidate['url'])])
        self.assertEqual(self.lib.parse_authors(self.lib.author_text()), ['旧作者'])

    def test_authors_are_persistent_and_deduplicated(self):
        self.assertEqual(json.loads(self.request('GET', '/api/dashboard')[2]),
                         dict(authors=[], profiles=[], entries=[], videos=[]))
        self.lib.authors.write_text('# 备注\n旧作者', encoding='utf-8')
        profiles = [profile('新作者', 'fixtureNewAccount'), profile('A', 'fixtureAccountA'),
                    profile('B', 'fixtureAccountB')]
        self.matches['搜索'] = profiles
        tokens = {entry['name']: entry['token'] for entry in self.search('搜索')}
        self.assertEqual(self.submit({'token': tokens['新作者']})[0], 201)
        tokens = {entry['name']: entry['token'] for entry in self.search('搜索')}
        with ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(lambda n: self.submit({'token': tokens[n]})[0], ('新作者', 'A', 'B')))
        self.assertEqual(statuses, [200, 201, 201])
        self.assertTrue(self.lib.author_text().startswith('# 备注\n旧作者\n'))
        self.assertEqual(sorted(self.lib.parse_authors(self.lib.author_text())), sorted(['旧作者','新作者','A','B']))
        self.assertCountEqual(self.saved_profiles(), [(p['name'], p['url']) for p in profiles])
        # A fresh Library reads verified links from disk and still accepts plain names in the legacy file.
        reopened = Library(self.lib.authors, self.lib.database, self.lib.media, self.lib.posters, self.lib.static)
        self.assertEqual(reopened.dashboard()['profiles'], self.lib.dashboard()['profiles'])

    def test_legacy_author_note_delete_and_readd(self):
        name = '城市/影像 ?#%2F'
        self.lib.authors.write_text('# 手工说明\n' + name + '\n' + name, encoding='utf-8')
        item, = self.entries()
        self.assertEqual((item['name'], item['url'], item['note']), (name, None, ''))
        self.assertIsInstance(item['version'], str)
        self.assertTrue(item['version'])
        status, body = self.change('PATCH', name, {'version': item['version'], 'note': '素材 #1'})
        self.assertEqual(status, 200, body)
        updated = body['entry']
        self.assertNotEqual(updated['version'], item['version'])
        self.assertEqual((updated['name'], updated['url'], updated['note']), (name, None, '素材 #1'))
        self.assertEqual(self.entries(), [updated])
        self.assertEqual(json.loads(self.request('GET', '/api/dashboard')[2])['entries'], [updated])
        self.assertEqual(self.lib.parse_authors(self.lib.author_text()), [name])
        self.assertEqual(self.change('DELETE', name, {'version': item['version']})[0], 409)
        status, body = self.change('PATCH', name, {'version': updated['version'], 'note': ''})
        self.assertEqual(status, 200, body)
        cleared = body['entry']
        self.assertEqual(cleared['note'], '')
        self.assertEqual(self.change('DELETE', name, {'version': cleared['version']}), (200, {'removed': True}))
        self.assertEqual(self.change('DELETE', name, {'version': cleared['version']})[0], 404)
        self.assertEqual(self.change('PATCH', name, {'version': cleared['version'], 'note': '旧页'})[0], 404)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.lib.author_text().splitlines(), ['# 手工说明'])
        reopened = Library(self.lib.authors, self.lib.database, self.lib.media, self.lib.posters, self.lib.static)
        self.assertEqual(reopened.dashboard()['entries'], [])
        self.matches[name] = [profile(name, 'readdedAuthor')]
        candidate, = self.search(name)
        self.assertEqual(self.submit({'token': candidate['token']})[0], 201)
        readded, = self.entries()
        self.assertEqual((readded['name'], readded['url'], readded['note']), (name, candidate['url'], ''))

    def test_verified_replacement_corrects_same_name_and_rejects_conflicts(self):
        self.lib.authors.write_text('旧作者\n', encoding='utf-8')
        self.matches['校正'] = [profile('新作者', 'firstID'), profile('新作者', 'secondID'),
                               profile('已监控', 'otherID'), profile('另一个名称', 'otherID')]
        first, second, other = self.search('校正')  # the last candidate shares the third identity
        self.assertEqual(self.submit({'token': other['token']})[0], 201)
        old = next(e for e in self.entries() if e['name'] == '旧作者')
        status, body = self.change('PATCH', '旧作者', dict(version=old['version'], token=first['token'], note='分类'))
        self.assertEqual(status, 200, body)
        new = body['entry']
        self.assertEqual((new['name'], new['url'], new['note']), ('新作者', first['url'], '分类'))
        status, body = self.change('PATCH', new['name'], dict(version=new['version'], token=second['token']))
        self.assertEqual(status, 200, body)
        corrected = body['entry']
        self.assertEqual((corrected['url'], corrected['note']), (second['url'], '分类'))
        self.assertEqual(self.saved_profiles(), [('新作者', second['url']), ('已监控', other['url'])])
        self.matches['冲突'] = [profile('另一显示名', 'otherID'), profile('已监控', 'newID')]
        before = self.lib.author_text()
        for candidate in [other, *self.search('冲突')]:
            status, _ = self.change('PATCH', corrected['name'],
                                    dict(version=corrected['version'], token=candidate['token'], note='错误的修改'))
            self.assertEqual(status, 409)
            self.assertEqual(self.lib.author_text(), before)

    def test_mutations_reject_unverified_or_cross_site_inputs(self):
        self.lib.authors.write_text('作者', encoding='utf-8')
        item, = self.entries()
        version = {'version': item['version']}
        for fields in (version, {}, {'note': '缺少版本'}, {**version, 'token': '伪造'},
                       {**version, 'note': None}, {**version, 'note': '换\n行'},
                       {**version, 'note': '长' * 201}, {**version, 'url': 'https://example.com/'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.change('PATCH', item['name'], fields)[0], 400)
        for data in ({}, {**version, 'name': '其他'}):
            self.assertEqual(self.change('DELETE', item['name'], data)[0], 400)
        for method, data in [('PATCH', {**version, 'note': '备注'}), ('DELETE', version)]:
            for headers, expected in [({'Origin':'https://example.com'}, 403), ({'Host':'bad.example'}, 403),
                                      ({'Sec-Fetch-Site':'cross-site'}, 403), ({'Content-Type':'text/plain'}, 415)]:
                with self.subTest(method=method, headers=headers):
                    self.assertEqual(self.change(method, item['name'], data, headers)[0], expected)
        self.assertEqual(self.entries(), [item])

    def test_overlapping_edits_do_not_overwrite_each_other(self):
        self.lib.authors.write_text('作者\n第二个', encoding='utf-8')
        first, second = self.entries()
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(lambda note: self.change('PATCH', first['name'],
                                    {'version': first['version'], 'note': note}), ['备注甲', '备注乙']))
        self.assertCountEqual([status for status, _ in answers], [200, 409])
        winner, = [data['entry'] for status, data in answers if status == 200]
        self.assertEqual(self.entries(), [winner, second])

    def test_write_requires_local_page_and_json(self):
        self.matches['作者'] = [profile('作者', 'fixtureAccount')]
        result, = self.search('作者')
        data = {'token': result['token']}
        self.assertEqual(self.submit(data, {'Origin':'https://example.com'})[0], 403)
        self.assertEqual(self.submit(data, {'Host':'remote.example'})[0], 403)
        self.assertEqual(self.submit(data, {'Content-Type':'text/plain'})[0], 415)
        self.assertFalse(self.lib.authors.exists())

    def test_search_only_invokes_desktop_for_local_get_with_a_single_query(self):
        path = '/api/author-search?q=fixture'
        for method in ('GET', 'HEAD'):
            for headers in ({'Origin': 'https://example.com'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(method=method, headers=headers):
                    self.assertEqual(self.request(method, path, headers=headers)[0], 403)
        self.assertEqual(self.request('HEAD', path)[0], 400)
        self.assertEqual(self.request('GET', path, headers={'Sec-Fetch-Dest': 'image'})[0], 400)
        self.assertEqual(self.request('GET', path + '&extra=1')[0], 400)
        self.assertEqual(self.request('GET', path + '&q=other')[0], 400)
        self.assertEqual(self.queries, [])

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
        self.lib.authors.write_text('历史作者\n', encoding='utf-8')
        active, = self.entries()
        self.assertEqual(self.change('DELETE', active['name'], {'version': active['version']})[0], 200)
        history = json.loads(self.request('GET', '/api/dashboard')[2])
        self.assertEqual(history['entries'], [])
        self.assertEqual(history['videos'], json.loads(body)['videos'])
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
