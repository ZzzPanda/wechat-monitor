"""Exercise the management API, scheduler and shared worker with individual push entries."""
import json
from http.client import HTTPConnection
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'dashboard' if (root/'dashboard').exists() else root/'state/dashboard-app'),
                str(root/'scripts')]
import crawl_worker
from server import DashboardServer, Library


class PublicWorkflow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.biz = 'PublicTest123'
        library = Library(*(folder/name for name in ('authors', 'videos.db', 'media', 'posters', 'static')))
        self.server = DashboardServer(('127.0.0.1', 0), library, public_search=lambda q: [
            {'id': self.biz, 'name': q, 'description': '编辑部'}])
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        (folder/'crawl-worker.key').write_text('local-worker-key-for-test-requests-2026')
        with patch.object(crawl_worker, 'MONITORING', folder):
            self.worker = crawl_worker.Dashboard()
        addr = 'http://%s:%s/api/crawl-worker' % self.server.server_address
        endpoint = patch.object(crawl_worker, 'ENDPOINT', addr)
        endpoint.start()
        self.addCleanup(endpoint.stop)

    def api(self, method, path, data=None, headers=None, expected=200):
        client = HTTPConnection(*self.server.server_address)
        try:
            client.request(method, path, json.dumps(data) if data is not None else None,
                           {'Content-Type': 'application/json', **(headers or {})})
            response = client.getresponse()
            body = json.load(response)
            self.assertEqual(response.status, expected, body)
            return body
        finally:
            client.close()

    def test_manage_accounts_over_http_and_read_removed_account_articles(self):
        self.server.public_selections.search = lambda q: [{'id': 'id'+q, 'name': q, 'description': '已核实'}]
        def select(name):
            return self.api('GET','/api/public/search?q='+name)['results'][0]['token']
        token = select('Source')
        add = self.api('POST','/api/public/accounts', {'token':token})
        self.assertTrue(add['added'])
        account = add['account']
        path = '/api/public/accounts/'+account['id']
        self.assertFalse(self.api('POST','/api/public/accounts',{'token':token})['added'])
        self.api('POST','/api/public/history',{'account_id':account['id'],'max_pages':1},expected=202)
        job = self.worker('claim')['job']
        self.worker('public_save', id=job['id'], article=dict(url='https://mp.weixin.qq.com/s/archive',
                    biz=account['biz'], mid='12345', idx=1, author='Source',title='Hello World', body='正文内容',
                    published='2026-11-20 09:00:00'))
        for data in [{}, {'version':account['version']}, {'version':account['version'],'note':None},
                     {'version':account['version'],'note':'前\n后'}, {'version':account['version'],'url':'伪造'},
                     {'version':account['version'],'token':'unverified'}]:
            self.api('PATCH',path,data,expected=400)
        note = self.api('PATCH',path,{'version':account['version'],'note':'待复核'})['account']
        self.api('DELETE',path,{'version':account['version']},expected=409)
        other = self.api('PATCH',path,{'version':note['version'],'token':select('Next')})['account']
        self.assertFalse(self.worker('public_active',id=job['id'])['active'])
        snapshot = self.api('GET','/api/public')
        self.assertEqual([a['id'] for a in snapshot['accounts']], [other['id']])
        self.assertEqual(snapshot['past_accounts'][0]['article_count'],1)
        query = '?account_id='+account['id']+'&sort=oldest&q=wOrLd&offset=0&limit=2'
        self.assertEqual(self.api('GET','/api/public/articles'+query)['total'], 1)
        self.api('GET','/api/public/articles?sort=unsupported',expected=400)
        self.api('GET','/api/public/articles?q=x&q=y',expected=400)
        self.api('POST','/api/public/history',{'account_id':account['id']},expected=404)
        restored = self.api('POST','/api/public/accounts',{'token':select('Source')})
        self.assertTrue(restored['added'])
        self.assertEqual(restored['account']['id'],account['id'])
        self.api('PATCH',path,{'version':restored['account']['version'],'token':select('Next')},expected=409)
        self.api('DELETE',path,{'version':note['version']},expected=409)
        self.assertTrue(self.api('DELETE',path,{'version':restored['account']['version']})['removed'])

    def test_recovered_alias_counts_as_one_success_and_unresolved_links_still_fail(self):
        choice = self.api('GET', '/api/public/search?q=Test')['results'][0]
        account = self.api('POST', '/api/public/accounts', {'token':choice['token']})['account']
        urls = [f'https://mp.weixin.qq.com/s?__biz={self.biz}&mid=123&idx=1&sn='+'ab'*16,
                'https://mp.weixin.qq.com/s/working']
        requests = []
        def read(url):
            requests.append(url)
            if url != urls[1]:
                raise ValueError('页面需要验证')
            return dict(url=url, biz=self.biz, mid='123', idx=1, author='Test', title='标题',
                        body='已核实正文', published='2026-11-20 21:00:00')
        def grid(entry, *, on_article, **options):
            for url in urls:
                on_article(url)
            return dict(complete=True, reason='到底')
        def run():
            self.server.public_articles.enqueue(account['id'])
            job = self.worker('claim')['job']
            crawl_worker.run_public_job(self.worker, job, grid, read)
            return next(j for j in self.api('GET', '/api/public')['jobs'] if j['id']==job['id'])

        first = run()
        self.assertEqual(requests, urls)
        self.assertEqual((first['status'], first['discovered'], first['saved'], first['failed']), ('done',1,1,0))
        requests.clear()
        urls.append(urls[0].replace('idx=1', 'idx=2'))
        second = run()
        self.assertEqual(requests, [urls[2]])
        self.assertEqual((second['status'], second['discovered'], second['skipped'], second['failed']),
                         ('partial',2,1,1))
        urls[:] = [urls[0]+'&idx=']
        rejected = run()
        self.assertEqual((rejected['status'], rejected['discovered']), ('partial',0))
        self.assertIn('未通过校验', rejected['error'])

    def test_archive_then_scheduled_new_sibling(self):
        choice = self.api('GET', '/api/public/search?q=Test')['results'][0]
        self.api('POST', '/api/public/accounts', {'token': 'unverified'}, expected=400)
        self.api('POST', '/api/authors', {'token': choice['token']}, expected=400)
        self.api('GET', '/api/public/search?q=Test', headers={'Sec-Fetch-Site':'cross-site'}, expected=403)
        account = self.api('POST', '/api/public/accounts', {'token':choice['token']})['account']
        self.assertEqual(account['biz'], self.biz)
        self.api('POST', '/api/public/history', {'account_id':account['id'], 'max_pages':2}, expected=202)
        history = self.worker('claim')['job']
        self.assertEqual((history['source'],history['max_pages']), ('public',2))
        links = ['https://mp.weixin.qq.com/s/test1','https://mp.weixin.qq.com/s/test2']
        def read(url):
            idx = int(url[-1])
            from datetime import datetime, timedelta, timezone
            published = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            return dict(url=url, biz=self.biz, mid='202611', idx=idx, author='Test',
                        title='标题'+str(idx), body=('文章'+str(idx))*5000, published=published)
        def grid(entry, *, on_article, **options):
            self.assertEqual(entry['biz'], self.biz)
            for url in links:
                self.assertFalse(on_article(url))
            return dict(complete=True,reason='结束')
        crawl_worker.run_public_job(self.worker, history, grid, read)
        self.api('PATCH','/api/public/settings',{'enabled':True,'interval_minutes':60,'start_at':int(time.time())})
        self.assertTrue(self.worker('tick')['queued'])
        incremental = self.worker('claim')['job']
        self.assertEqual(incremental['mode'],'incremental')
        links.append('https://mp.weixin.qq.com/s/test3')
        seen = []
        def read_new(url):
            seen.append(url)
            return read(url)
        crawl_worker.run_public_job(self.worker,incremental,grid,read_new)
        self.assertEqual(seen,links[2:])
        overview = self.api('GET','/api/public')
        latest = next(j for j in overview['jobs'] if j['id']==incremental['id'])
        self.assertEqual((latest['status'],latest['saved'],latest['skipped']),('done',1,2))
        page = self.api('GET','/api/public/articles?account_id='+account['id'])
        self.assertEqual(page['total'],3)
        for item in page['articles']:
            full = self.api('GET','/api/public/articles/'+item['id'])
            self.assertEqual(full['body'],('文章'+str(full['idx']))*5000)
            self.assertEqual(full['url'],links[full['idx']-1])
