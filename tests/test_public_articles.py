from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'dashboard'))
from public_articles import AccountConflict, PublicArticles, digest
from wechat_public_article import ArticlePage, public_url, read_article

BIZ = 'Qm9va3M='
ARTICLE = dict(biz=BIZ, mid='2651615288', idx=1, author='示例周刊', title='同一标题',
               published='2026-11-20 21:00:00', url='https://mp.weixin.qq.com/s/example',
               body='第一段\n同标题的另一篇也要保存。')


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = PublicArticles(Path(temp.name) / 'articles.sqlite3')
        self.account = self.store.add_account(dict(biz=BIZ, name='示例周刊'))
        self.account_id = self.account['id']

    def start(self, mode='history', **params):
        requested = self.store.enqueue(self.account_id, mode, **params)
        claimed = self.store.claim()
        self.assertEqual(requested['id'], claimed['id'])
        return claimed

    def test_stable_id_and_job_counters_survive_retry_alias_and_restart(self):
        job_id = self.start()['id']
        # Concurrent callbacks for an original link and an alternate public short link.
        alias = dict(ARTICLE, url='https://mp.weixin.qq.com/s/alias')
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda record: self.store.save(job_id, record), (ARTICLE, alias)))
        self.assertCountEqual(statuses, ['saved', 'skipped'])
        self.assertTrue(self.store.known_url(self.account_id, alias['url']))
        second = dict(ARTICLE, idx=2, url='https://mp.weixin.qq.com/s/second')
        self.store.fail(job_id, second['url'], '读取中断')
        self.assertEqual(self.store.snapshot()['jobs'][0]['failed'], 1)
        self.assertTrue(self.store.yield_history(job_id, {'position': 3}))
        self.assertEqual(self.store.claim()['checkpoint'], {'position': 3})
        self.store.save(job_id, second)
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.claim()['id'], job_id)
        self.assertEqual(self.store.skip_known(job_id, second['url']), second['published'])
        finished = self.store.finish(job_id, 'done')
        self.assertEqual([finished[x] for x in ('status','saved','discovered','failed')], ['done',2,2,0])
        archive = self.store.list_articles(self.account_id, limit=1, offset=1)
        self.assertEqual(archive['total'], 2)
        self.assertNotIn('body', archive['articles'][0])
        self.assertEqual(self.store.article(archive['articles'][0]['id'])['body'], second['body'])
        repeat = self.start()['id']
        self.assertEqual(self.store.skip_known(repeat, alias['url']), ARTICLE['published'])
        self.store.save(repeat, second)
        self.assertEqual(self.store.finish(repeat, 'done')['skipped'], 2)

    def test_wrong_source_conflicting_url_and_cancel(self):
        job = self.start()
        self.store.save(job['id'], ARTICLE)
        for data in [dict(ARTICLE, biz='other'), dict(ARTICLE, idx=2), dict(ARTICLE, url='https://example.com/s/abc'),
                     dict(ARTICLE, body='  ')]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.store.save(job['id'], data)
        self.store.cancel(job['id'])
        with self.assertRaises(ValueError):
            self.store.save(job['id'], ARTICLE)
        self.assertEqual(self.store.list_articles()['total'], 1)

    def test_cutoff_only_advances_after_a_completed_incremental_job(self):
        # After initial creation, parent Queue schedules on claim too; pin its next run in the future.
        self.store.configure(False, 60)
        added = self.account['added_at']
        start = added + 2*3600
        with patch('time.time', return_value=start):
            first = self.start('incremental')
            self.assertEqual(first['since'], added//60*60)
            old = dict(ARTICLE, published='2020-01-01 00:00:00')
            self.store.skip_before(first['id'], old)
            self.store.finish(first['id'], 'partial')
        with patch('time.time', return_value=start+3600):
            retry = self.start('incremental')
            self.assertEqual(retry['since'], first['since'])
            self.store.finish(retry['id'], 'done')
        with patch('time.time', return_value=start+7200):
            last = self.start('incremental')
            self.assertEqual(last['since'], (start+3600)//60*60)
        self.assertEqual(self.store.list_articles()['total'], 0)

    def test_history_limit_persists_after_recovery(self):
        with self.assertRaises(ValueError):
            self.store.enqueue(self.account_id, 'history', True)
        limited = self.start(max_pages=2)
        self.assertEqual(limited['max_pages'], 2)
        self.assertEqual(self.store.enqueue(self.account_id, max_pages=2)['id'], limited['id'])
        full = self.store.enqueue(self.account_id)
        self.assertNotEqual(full['id'], limited['id'])
        self.assertFalse(self.store.yield_history(limited['id'], {'position': 1}))
        self.store.recover()
        self.assertEqual(self.store.claim()['max_pages'], 2)

    def test_reports_only_unverified_siblings_and_incremental_backfills_known_push(self):
        self.store.configure(False, 60)
        # Two different, partly archived pushes in past jobs.
        old = dict(ARTICLE, published='2020-01-01 02:00:00')
        other = dict(old, mid='1234', idx=8, url='https://mp.weixin.qq.com/s/other')
        first = self.start()['id']
        self.store.save(first, other)
        self.store.finish(first, 'done')
        recent = self.start()['id']
        for idx in (2, 3):
            self.store.save(recent, dict(old, idx=idx, url=f'https://mp.weixin.qq.com/s/part{idx}'))
        result = self.store.finish(recent, 'done', '已检查指定的 2 屏文章')
        self.assertEqual(result['status'], 'done')
        self.assertIn('已检查指定的 2 屏文章', result['error'])
        self.assertIn('推送序号未核实', result['error'])
        self.assertIn(f"{old['mid']} 的第 1 篇", result['error'])
        self.assertNotIn(other['mid'], result['error'])
        timer = self.start('incremental')['id']
        self.assertEqual(self.store.skip_before(timer, old), 'saved')
        self.assertEqual(self.store.skip_before(timer, dict(old, url='https://mp.weixin.qq.com/s/alias')), 'skipped')
        self.assertEqual(self.store.skip_before(timer, dict(old, mid='123456', url='https://mp.weixin.qq.com/s/unrelated')), 'skipped')
        finished = self.store.finish(timer, 'done', '增量完成')
        self.assertEqual((finished['status'], finished['saved'], finished['skipped'], finished['error']),
                         ('done', 1, 1, '增量完成'))
        self.assertEqual(self.store.list_articles()['total'], 4)

    def test_unverified_indices_are_bounded_ranges_and_preserve_failures(self):
        job = self.start()['id']
        for mid in range(100, 109):
            self.store.save(job, dict(ARTICLE, mid=str(mid), idx=2147483647,
                                      url=f'https://mp.weixin.qq.com/s/m{mid}'))
        self.store.fail(job, 'https://mp.weixin.qq.com/s/failed', '正文读取失败')
        result = self.store.finish(job, 'done')
        self.assertEqual((result['status'], result['failed']), ('partial', 1))
        self.assertIn('1–2147483646', result['error'])
        self.assertIn('仅列前 6 处', result['error'])
        self.assertLess(len(result['error']), 400)

    def test_rebind_cancels_jobs_and_preserves_account_and_article_identity(self):
        self.store.configure(False, 60)
        job = self.start()
        self.store.save(job['id'], ARTICLE)
        waiting = self.store.enqueue(self.account_id, 'incremental')
        edited = self.store.update_account(self.account_id, self.account['version'], note='编辑部')
        self.assertNotEqual(edited['version'], self.account['version'])
        choice = dict(name='新刊', biz='anotherBiz', description='已核实主页')
        replacement = self.store.update_account(self.account_id, edited['version'], candidate=choice)
        self.assertNotEqual(replacement['id'], self.account_id)
        self.assertEqual(replacement['note'], '编辑部')
        new = self.store.snapshot()
        self.assertEqual([a['id'] for a in new['accounts']], [replacement['id']])
        self.assertEqual([(a['id'],a['article_count']) for a in new['past_accounts']], [(self.account_id,1)])
        self.assertEqual({(j['id'],j['status']) for j in new['jobs']},
                         {(job['id'],'cancelled'),(waiting['id'],'cancelled')})
        with self.assertRaises(ValueError):
            self.store.save(job['id'], dict(ARTICLE, idx=2))
        with self.assertRaises(KeyError):
            self.store.enqueue(self.account_id)
        self.assertIsNone(self.store.claim())
        archived = self.store.list_articles(self.account_id)['articles'][0]
        self.assertEqual(self.store.article(archived['id'])['body'], ARTICLE['body'])
        self.assertEqual(self.store.list_articles(replacement['id'])['total'], 0)
        restored = self.store.add_account(dict(biz=BIZ, name='恢复名称'))
        self.assertEqual((restored['id'], restored['added'], restored['article_count'], restored['note']),
                         (self.account_id, True, 1, '编辑部'))
        self.assertEqual(self.store.snapshot()['past_accounts'], [])
        with self.assertRaises(AccountConflict):
            self.store.remove_account(self.account_id, edited['version'])
        with self.assertRaises(AccountConflict):
            self.store.update_account(self.account_id, restored['version'], candidate=choice, note='不能写入')
        self.assertEqual(next(a for a in self.store.accounts() if a['id']==self.account_id)['note'], '编辑部')
        self.store.remove_account(replacement['id'], replacement['version'])
        self.assertEqual(self.store.snapshot()['past_accounts'], [])

    def test_concurrent_updates_reject_stale_version_and_remove_aba(self):
        def update(note):
            try:
                return self.store.update_account(self.account_id, self.account['version'], note=note)['version']
            except AccountConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            versions = list(pool.map(update, ['甲', '乙']))
        self.assertEqual(versions.count(None), 1)
        version = next(v for v in versions if v)
        self.store.remove_account(self.account_id, version)
        self.assertFalse(self.store.accounts())
        old = self.account['added_at']
        with patch('time.time', return_value=old+9000):
            active = self.store.add_account(dict(name='示例周刊',biz=BIZ))
            same = self.store.add_account(dict(name='过期缓存中的旧号名',biz=BIZ))
            self.assertEqual((active['id'], active['article_count'], active['added_at']), (self.account_id,0,old+9000))
            self.assertEqual((same['added'], same['version'], same['name']), (False, active['version'], '示例周刊'))
            self.assertEqual(self.start('incremental')['since'], active['added_at']//60*60)

    def test_global_article_search_and_order_precede_pagination(self):
        job = self.start()
        for idx, title in enumerate(['Z first', 'A %_STRASSE', 'B Straße'], 1):
            self.store.save(job['id'], dict(ARTICLE, idx=idx, title=title, url=f'https://mp.weixin.qq.com/s/{idx}',
                                            published=f'2026-11-2{idx} 00:00:00'))
        self.store.finish(job['id'],'done')
        other = self.store.add_account(dict(name='二刊',biz='another'))
        self.store.enqueue(other['id'])
        next_job = self.store.claim()
        self.store.save(next_job['id'], dict(ARTICLE, biz='another', author='Straße', title='C last',
                                              url='https://mp.weixin.qq.com/s/extra'))
        result = self.store.list_articles(q='straße', sort='title',limit=1,offset=2)
        self.assertEqual((result['total'], result['articles'][0]['title']), (3, 'C last'))
        self.assertEqual(self.store.list_articles(q='%_')['total'], 1)
        for sort, title in [('oldest','Z first'),('newest','B Straße'),('title','A %_STRASSE')]:
            self.assertEqual(self.store.list_articles(self.account_id,sort=sort,limit=1)['articles'][0]['title'], title)

    def test_migrate_unmodified_old_account_and_article_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('''CREATE TABLE accounts(id TEXT PRIMARY KEY,biz TEXT UNIQUE NOT NULL,name TEXT NOT NULL,
                              description TEXT NOT NULL,added_at INTEGER NOT NULL)''')
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)', (self.account_id, BIZ, '旧公众号', '学校', 1234))
                db.execute('''CREATE TABLE articles(id TEXT PRIMARY KEY,account_id TEXT,biz TEXT,mid TEXT,idx INTEGER,
                            author TEXT,url TEXT,title TEXT,published TEXT,body TEXT,UNIQUE(biz,mid,idx))''')
                db.execute('INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (digest('legacy'),self.account_id,BIZ,'123',1,'旧公众号',ARTICLE['url'],
                            '旧标题',ARTICLE['published'],'旧正文'))
            archive = PublicArticles(path)
            prior = archive.snapshot()['accounts'][0]
            self.assertEqual((prior['id'], prior['added_at'], prior['article_count'], prior['note']),
                             (self.account_id,1234,1,''))
            self.assertEqual(len(prior['version']),32)
            self.assertEqual(PublicArticles(path).accounts()[0]['version'],prior['version'])
            self.assertEqual(archive.article(digest('legacy'))['body'],'旧正文')


class PageTest(unittest.TestCase):
    def test_blank_dom_retried_once_without_retrying_verification_or_deletion(self):
        complete = ('''<script>var biz="Qm9va3M=";var mid="123";var idx="2";var ct="1795179600";</script>
            <h1 id="activity-name">标题</h1><a id="js_name">示例周刊</a>
            <div id="js_content">正文内容</div>''').encode()
        for documents, success, calls in [
                ([b'<html><head><title>WeChat</title></head><body></body></html>', complete], True, 2),
                ([b'<body></body>', b'<body></body>'], False, 2),
                (['<body>环境异常，请完成验证</body>'.encode(), complete], False, 1),
                (['<body>该内容已被删除</body>'.encode(), complete], False, 1)]:
            with self.subTest(success=success, calls=calls), tempfile.TemporaryDirectory() as folder:
                (Path(folder) / 'tmp').mkdir()
                with patch('wechat_paths.DATA_ROOT', Path(folder)), patch('shutil.which', return_value=sys.executable), \
                        patch('time.sleep'), patch('subprocess.run', side_effect=[
                            SimpleNamespace(stdout=d) for d in documents]) as run:
                    if success:
                        self.assertEqual(read_article(ARTICLE['url'])['body'], '正文内容')
                    else:
                        with self.assertRaises(ValueError):
                            read_article(ARTICLE['url'])
                    self.assertEqual(run.call_count, calls)
                    if calls == 2:
                        self.assertEqual(run.call_args_list[0].args, run.call_args_list[1].args)

    def result(self, content, fields=True):
        page = ArticlePage()
        if fields:
            page.feed('''<script>var biz="Qm9va3M=";var mid="123";var idx="2";var ct="1795179600";</script>
                        <h1 id="activity-name">标题</h1><a id="js_name">示例周刊</a>
                        <time id="publish_time">2026年11月20日 21:00</time>''')
        page.feed(content)
        page.close()
        return page.result(ARTICLE['url'])

    def test_public_dom_fields_body_and_image_only_posts(self):
        article = self.result('<div>页面外</div><div id="js_content"><p>首段<span>有词</span></p>'
                              '<p>另段<br>换行</p><script>不给读者的代码</script></div>')
        self.assertEqual((article['idx'], article['published'], article['body']),
                         (2, '2026-11-20 21:00:00', '首段有词\n另段\n换行'))
        pic = self.result('<div id="js_content"><img data-src="https://mmbiz.qpic.cn/id1"></div>')
        self.assertIn('https://mmbiz.qpic.cn/id1', pic['body'])
        with self.assertRaises(ValueError):
            self.result('<p>环境异常，请完成验证</p>', False)

    def test_preview_with_restricted_reading_control_is_not_a_complete_article(self):
        preview = '<div id="js_content"><p>公开的导读。</p></div>'
        for control in ('<div id="js_pay_area"><span>付费后</span><b>阅读剩余 70% 内容</b></div>',
                        '<div role="dialog"><button>登录后继续阅读</button></div>',
                        '<a class="weui-btn">登录后查看全文</a>'):
            with self.subTest(control=control), self.assertRaisesRegex(ValueError, '未取得全文'):
                self.result(preview + control)
        body = '<div id="js_content"><p>书评介绍了“付费后阅读剩余内容”。</p><button>登录后阅读全文</button></div>'
        for neutral in ('<p>示例界面写着付费后阅读剩余内容</p>',
                        '<button>登录后查看留言</button>', '<button>联系客服</button>',
                        '<div hidden><div id="js_pay_area">付费后继续阅读</div></div>',
                        '<div aria-hidden="true"><button>付费后查看全文</button></div>',
                        '<div style="display: none!important;"><button>登录后继续阅读</button></div>',
                        '<template><button>付费后继续阅读</button></template>'):
            with self.subTest(neutral=neutral):
                self.assertIn('书评', self.result(body + neutral)['body'])

    def test_only_article_links_and_public_parameters(self):
        self.assertEqual(public_url(ARTICLE['url'] + '?tracking=sample'), ARTICLE['url'])
        long = 'https://mp.weixin.qq.com/s?__biz=Qm9va3M%3D&mid=123&idx=2&sn=' + 'ab'*16
        self.assertEqual(public_url(long + '&from=tracking'), long)
        for invalid in ['https://mp.weixin.qq.com.evil.com/s/one', 'https://mp.weixin.qq.com/mp/profile',
                        'https://mp.weixin.qq.com/s?__biz=Qm9va3M%3D&mid=123&idx=2']:
            with self.assertRaises(ValueError):
                public_url(invalid)
