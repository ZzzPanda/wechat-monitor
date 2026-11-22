"""Transactional archive and collection jobs for public WeChat accounts."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from account_search import valid_text
from crawl_queue import Queue, job_row

BEIJING = timezone(timedelta(hours=8))
BIZ = re.compile(r'[A-Za-z0-9_+/=-]{1,128}')
NUMBER = re.compile(r'[1-9][0-9]{0,19}')
MAX_HISTORY_PAGES = 100000
ARTICLE_FIELDS = 'a.id,a.account_id,a.author,a.url,a.title,a.published,a.biz,a.mid,a.idx'
ACCOUNT_SELECT = '''SELECT a.*, (SELECT count(*) FROM articles p WHERE p.account_id=a.id)
                    AS article_count FROM accounts a'''


class AccountConflict(Exception):
    """The client edited a stale record or selected an already monitored identity."""


TABLES = (
    '''CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY, biz TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        description TEXT NOT NULL, added_at INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '', monitored INTEGER NOT NULL DEFAULT 1,
        version TEXT NOT NULL DEFAULT '')''',
    '''CREATE TABLE IF NOT EXISTS job_options (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id), max_pages INTEGER,
        started_at INTEGER, since INTEGER)''',
    '''CREATE TABLE IF NOT EXISTS articles (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        biz TEXT NOT NULL, mid TEXT NOT NULL, idx INTEGER NOT NULL,
        author TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
        published TEXT NOT NULL, body TEXT NOT NULL,
        UNIQUE(biz,mid,idx))''',
    'CREATE INDEX IF NOT EXISTS article_dates ON articles(account_id,published DESC)',
    'CREATE INDEX IF NOT EXISTS article_all_dates ON articles(published DESC)',
    '''CREATE TABLE IF NOT EXISTS article_urls (
        url TEXT PRIMARY KEY, article_id TEXT NOT NULL REFERENCES articles(id))''',
)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def verified_url(url):
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError('无效的微信文章地址。')
    parts = urlsplit(url)
    if parts.scheme == 'https' and parts.netloc == 'mp.weixin.qq.com' and not parts.fragment:
        if re.fullmatch(r'/s/[A-Za-z0-9_-]{1,128}', parts.path) and not parts.query:
            return url
        if parts.path == '/s':
            q = parse_qs(parts.query, keep_blank_values=True)
            if (set(q) == {'__biz', 'mid', 'idx', 'sn'} and all(len(v) == 1 for v in q.values())
                    and BIZ.fullmatch(q['__biz'][0]) and NUMBER.fullmatch(q['mid'][0])
                    and NUMBER.fullmatch(q['idx'][0]) and re.fullmatch('[a-fA-F0-9]{32}', q['sn'][0])):
                return url
    raise ValueError('需要公开的微信文章链接。')


def validate_article(item):
    if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in
            ('biz', 'mid', 'author', 'title', 'published', 'body', 'url')):
        raise ValueError('文章数据不完整。')
    if (not BIZ.fullmatch(item['biz']) or not NUMBER.fullmatch(item['mid']) or
            type(item.get('idx')) is not int or not 0 < item['idx'] <= 2**31-1 or
            not valid_text(item['author'], 128) or not valid_text(item['title'], 1024)
            or not item['body'].strip() or len(item['body']) > 2_000_000):
        raise ValueError('文章身份、标题或正文无效。')
    verified_url(item['url'])
    if urlsplit(item['url']).path == '/s':
        q = parse_qs(urlsplit(item['url']).query)
        if (q['__biz'][0], q['mid'][0], int(q['idx'][0])) != (item['biz'], item['mid'], item['idx']):
            raise ValueError('文章链接与正文中的公众号或图文序号不一致。')
    stamp = datetime.strptime(item['published'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING)
    if stamp.strftime('%Y-%m-%d %H:%M:%S') != item['published']:
        raise ValueError('文章发布时间格式无效。')
    return int(stamp.timestamp())


class PublicArticles(Queue):
    def __init__(self, path):
        super().__init__(path)
        self._article_ready = False
        self._article_lock = threading.Lock()

    @contextmanager
    def db(self, write=False):
        if not self._article_ready:
            with self._article_lock:
                if not self._article_ready:
                    with super().db(write=True) as conn:
                        for statement in TABLES:
                            conn.execute(statement)
                        columns = {r['name'] for r in conn.execute('PRAGMA table_info(accounts)')}
                        for name, declaration in [('note', "TEXT NOT NULL DEFAULT ''"),
                                                  ('monitored', 'INTEGER NOT NULL DEFAULT 1'),
                                                  ('version', "TEXT NOT NULL DEFAULT ''")]:
                            if name not in columns:
                                conn.execute(f'ALTER TABLE accounts ADD COLUMN {name} {declaration}')
                        conn.execute("UPDATE accounts SET version=lower(hex(randomblob(16))) WHERE version=''")
                    self._article_ready = True
        with super().db(write=write) as conn:
            yield conn

    def accounts(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(ACCOUNT_SELECT + ' WHERE a.monitored=1 ORDER BY a.name,a.id')]

    @staticmethod
    def _identity(candidate):
        if not isinstance(candidate, dict):
            raise ValueError('请选择核实的公众号。')
        biz, name, desc = candidate.get('biz'), candidate.get('name'), candidate.get('description', '')
        if (not isinstance(biz, str) or not BIZ.fullmatch(biz) or not valid_text(name, 128)
                or not isinstance(desc, str) or (desc and not valid_text(desc, 1024))):
            raise ValueError('公众号身份或名称无效。')
        return digest(biz), biz, name.strip(), desc.strip()

    @staticmethod
    def _monitor(db, identity, previous, note):
        account_id, biz, name, description = identity
        now = int(time.time())
        if previous is None:
            db.execute('''INSERT INTO accounts(id,biz,name,description,added_at,note,version)
                       VALUES(?,?,?,?,?,?,?)''', (account_id, biz, name, description, now, note, uuid.uuid4().hex))
        elif (not previous['monitored'] or
              (previous['name'], previous['description'], previous['note']) != (name, description, note)):
            db.execute('''UPDATE accounts SET name=?,description=?,note=?,version=?,monitored=1,added_at=?
                          WHERE id=?''', (name, description, note, uuid.uuid4().hex,
                                         previous['added_at'] if previous['monitored'] else now, account_id))
            db.execute("UPDATE jobs SET author=? WHERE profile_url=? AND status='queued'", (name, account_id))
        return dict(db.execute(ACCOUNT_SELECT + ' WHERE a.id=?', (account_id,)).fetchone())

    def add_account(self, candidate):
        identity = self._identity(candidate)
        with self.db(write=True) as db:
            previous = db.execute('SELECT * FROM accounts WHERE id=?', (identity[0],)).fetchone()
            if previous and previous['monitored']:
                account = db.execute(ACCOUNT_SELECT + ' WHERE a.id=?', (identity[0],)).fetchone()
                return dict(account, added=False)
            account = self._monitor(db, identity, previous, previous['note'] if previous else '')
            return dict(account, added=True)

    @staticmethod
    def _current(db, account_id, version):
        if not valid_text(version, 256):
            raise ValueError('请提供公众号当前的版本。')
        row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        if not row['monitored'] or row['version'] != version:
            raise AccountConflict('这条公众号监控记录已经变更，请刷新后重试。')
        return row

    @staticmethod
    def _unmonitor(db, account_id):
        db.execute('UPDATE accounts SET monitored=0,version=? WHERE id=?', (uuid.uuid4().hex, account_id))
        db.execute("""UPDATE jobs SET status='cancelled',error='公众号已移除或更换，采集已停止。',updated_at=?
                      WHERE profile_url=? AND status IN ('running','queued')""", (int(time.time()), account_id))

    def remove_account(self, account_id, version):
        with self.db(write=True) as db:
            self._current(db, account_id, version)
            self._unmonitor(db, account_id)

    def update_account(self, account_id, version, *, note=None, candidate=None):
        if note is not None and note != '' and not valid_text(note, 200):
            raise ValueError('备注最多 200 个字符，请使用单行文字。')
        identity = self._identity(candidate) if candidate is not None else None
        with self.db(write=True) as db:
            current = self._current(db, account_id, version)
            previous = current
            if identity is None:
                identity = (account_id, current['biz'], current['name'], current['description'])
            elif identity[0] != account_id:
                previous = db.execute('SELECT * FROM accounts WHERE id=?', (identity[0],)).fetchone()
                if previous and previous['monitored']:
                    raise AccountConflict('所选公众号已在监控名单中。')
                self._unmonitor(db, account_id)
            return self._monitor(db, identity, previous, current['note'] if note is None else note.strip())

    @staticmethod
    def _entries(db):
        return [dict(row) for row in db.execute('SELECT name,id AS url FROM accounts WHERE monitored=1 ORDER BY name,id')]

    def schedule(self, now=None):
        with self.db(write=True) as db:
            return self._schedule_due(db, self._entries(db), int(time.time() if now is None else now))

    def enqueue(self, account_id, mode='history', max_pages=None):
        if max_pages is not None and (type(max_pages) is not int or
                not 1 <= max_pages <= MAX_HISTORY_PAGES or mode != 'history'):
            raise ValueError('历史页数须为有效正整数。')
        with self.db(write=True) as db:
            account = db.execute('SELECT * FROM accounts WHERE monitored=1 AND id=?', (account_id,)).fetchone()
            if account is None:
                raise KeyError(account_id)
            now = int(time.time())
            if mode == 'history':
                # A bounded backfill cannot satisfy a request for the entire history.
                existing = db.execute("""SELECT j.id FROM jobs j JOIN job_options o ON o.job_id=j.id
                    WHERE j.profile_url=? AND j.mode='history' AND j.status IN ('running','queued')
                    AND o.max_pages IS ?""", (account_id, max_pages)).fetchone()
                job_id = existing[0] if existing else uuid.uuid4().hex
                if not existing:
                    db.execute("""INSERT INTO jobs(id,author,profile_url,mode,status,created_at,updated_at)
                                 VALUES(?,?,?,'history','queued',?,?)""",
                               (job_id, account['name'], account_id, now, now))
            else:
                job_id = self._enqueue(db, dict(name=account['name'], url=account_id), mode, now)
            db.execute('INSERT OR IGNORE INTO job_options(job_id,max_pages) VALUES(?,?)', (job_id, max_pages))
            job = job_row(db, job_id)
            job.update(account_id=account_id, max_pages=db.execute(
                'SELECT max_pages FROM job_options WHERE job_id=?', (job_id,)).fetchone()[0])
            return job

    def claim(self):
        # Membership, claiming and cancellation share a transaction with the list mutations.
        with self.db(write=True) as db:
            now = int(time.time())
            self._schedule_due(db, self._entries(db), now)
            if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                return None
            row = db.execute("""SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.profile_url
                                WHERE j.status='queued' AND a.monitored=1 ORDER BY
                                CASE j.mode WHEN 'incremental' THEN 0 ELSE 1 END,j.created_at,j.rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',error='',updated_at=? WHERE id=?", (now, row['id']))
            job = job_row(db, row['id'])
            if job['mode'] == 'history':
                saved = db.execute('SELECT cursor FROM history_checkpoints WHERE job_id=?', (job['id'],)).fetchone()
                job['checkpoint'] = json.loads(saved['cursor']) if saved else None
            account = dict(db.execute('SELECT * FROM accounts WHERE id=?', (job['profile_url'],)).fetchone())
            db.execute('INSERT OR IGNORE INTO job_options(job_id) VALUES(?)', (job['id'],))
            last = db.execute("""SELECT max(o.started_at) FROM jobs j JOIN job_options o ON o.job_id=j.id
                               WHERE j.profile_url=? AND j.mode='incremental' AND j.status='done'""",
                              (account['id'],)).fetchone()[0]
            since = max(last or 0, account['added_at']) if job['mode'] == 'incremental' else None
            if since is not None:
                since -= since % 60  # Some articles expose their publishing time only to the minute.
            db.execute('''UPDATE job_options SET started_at=?,since=? WHERE job_id=? AND started_at IS NULL''',
                       (int(time.time()), since, job['id']))
            opts = db.execute('SELECT max_pages,since FROM job_options WHERE job_id=?', (job['id'],)).fetchone()
            return dict(job, account=account, account_id=account['id'], **opts)

    def snapshot(self):
        result = super().snapshot()
        with self.db() as db:
            accounts = [dict(row) for row in db.execute(ACCOUNT_SELECT + ' ORDER BY a.name,a.id')]
            result['accounts'] = [a for a in accounts if a['monitored']]
            result['past_accounts'] = [a for a in accounts if not a['monitored'] and a['article_count']]
            limits = dict(db.execute('SELECT job_id,max_pages FROM job_options WHERE max_pages IS NOT NULL'))
        for job in result['jobs']:
            job.update(account_id=job['profile_url'], max_pages=limits.get(job['id']))
        return result

    def yield_history(self, job_id, checkpoint):
        if not isinstance(checkpoint, dict):
            raise ValueError('历史采集断点无效。')
        cursor = json.dumps(checkpoint, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(cursor) > 16_384:
            raise ValueError('历史采集断点过大。')
        with self.db(write=True) as db:
            moved = db.execute("""UPDATE jobs SET status='queued',error='等待定时增量完成后继续',updated_at=?
                    WHERE id=? AND mode='history' AND status='running' AND EXISTS (
                    SELECT 1 FROM job_options WHERE job_id=? AND max_pages IS NULL)""",
                               (int(time.time()), job_id, job_id))
            if not moved.rowcount:
                return False
            db.execute('INSERT OR REPLACE INTO history_checkpoints(job_id,cursor) VALUES(?,?)', (job_id, cursor))
            return True

    @staticmethod
    def _job(db, job_id):
        job = db.execute('''SELECT j.status,j.mode,j.profile_url AS account_id,a.biz,o.since FROM jobs j
                JOIN accounts a ON a.id=j.profile_url LEFT JOIN job_options o ON o.job_id=j.id
                WHERE j.id=?''', (job_id,)).fetchone()
        if job is None:
            raise KeyError(job_id)
        if job['status'] != 'running':
            raise ValueError('公众号采集任务已停止。')
        return job

    @staticmethod
    def _mark(db, job_id, key, url, status, error=''):
        # A failed read initially has only a URL; resolve it to the stable article key after retry.
        unresolved = 'url:' + digest(url)
        if key != unresolved:
            db.execute("DELETE FROM items WHERE job_id=? AND work_key=? AND status='failed'", (job_id, unresolved))
        db.execute('''INSERT INTO items(job_id,work_key,url,status,error) VALUES(?,?,?,?,?)
                      ON CONFLICT(job_id,work_key) DO UPDATE SET status=excluded.status,error=excluded.error
                      WHERE items.status NOT IN ('saved','skipped')''', (job_id, key, url, status, error))
        db.execute('UPDATE jobs SET updated_at=? WHERE id=?', (int(time.time()), job_id))

    def fail(self, job_id, url, error):
        url = verified_url(url)
        with self.db(write=True) as db:
            self._job(db, job_id)
            self._mark(db, job_id, 'url:' + digest(url), url, 'failed', str(error)[:500])

    @staticmethod
    def _by_url(db, account_id, url):
        return db.execute('''SELECT a.id,a.published FROM article_urls u JOIN articles a ON u.article_id=a.id
                            WHERE u.url=? AND a.account_id=?''', (url, account_id)).fetchone()

    def known_url(self, account_id, url):
        with self.db() as db:
            return self._by_url(db, account_id, url) is not None

    def skip_known(self, job_id, url):
        url = verified_url(url)
        with self.db(write=True) as db:
            job = self._job(db, job_id)
            existing = self._by_url(db, job['account_id'], url)
            if existing:
                self._mark(db, job_id, existing['id'], url, 'skipped')
                return existing['published']
            return None

    def save(self, job_id, article):
        return self._store(job_id, article, before=False)

    def skip_before(self, job_id, article):
        return self._store(job_id, article, before=True)

    def finish(self, job_id, status, error=''):
        if status != 'cancelled':
            with self.db() as db:
                # Only assess pushes touched in this job, using every archived
                # sibling. Consecutive pairs describe gaps without enumerating idx.
                gaps = db.execute("""
                    WITH touched AS (
                        SELECT DISTINCT a.biz,a.mid FROM items i JOIN articles a ON a.id=i.work_key
                        WHERE i.job_id=? AND i.status IN ('saved','skipped')
                    ), numbered AS (
                        SELECT a.mid,a.idx,lag(a.idx,1,0) OVER (
                            PARTITION BY a.biz,a.mid ORDER BY a.idx) AS prior
                        FROM touched t JOIN articles a ON a.biz=t.biz AND a.mid=t.mid
                    )
                    SELECT mid,prior+1 AS first,idx-1 AS last FROM numbered
                    WHERE idx>prior+1 ORDER BY mid DESC,idx LIMIT 7
                """, (job_id,)).fetchall()
            if gaps:
                groups = {}
                for gap in gaps[:6]:
                    first, last = gap['first'], gap['last']
                    numbers = str(first) if first == last else f'{first}–{last}'
                    groups.setdefault(gap['mid'], []).append(numbers)
                detail = '；'.join(f'{mid} 的第 {"、".join(numbers)} 篇' for mid, numbers in groups.items())
                if len(gaps) > 6:
                    detail += '（仅列前 6 处）'
                error = '；'.join(filter(None, (error, '推送序号未核实：' + detail)))
        return super().finish(job_id, status, error)

    def _store(self, job_id, a, before):
        published_at = validate_article(a)
        key = digest(f"{a['biz']}:{a['mid']}:{a['idx']}")
        with self.db(write=True) as db:
            job = self._job(db, job_id)
            if job['biz'] != a['biz']:
                raise ValueError('该文章的公众号标识与所选公众号不一致。')
            if before:
                if job['mode'] != 'incremental' or job['since'] is None or published_at >= job['since']:
                    raise ValueError('该文章未早于本次增量采集的起点。')
                known_push = db.execute('SELECT 1 FROM articles WHERE biz=? AND mid=? LIMIT 1',
                                        (a['biz'], a['mid'])).fetchone()
                if not known_push:
                    self._mark(db, job_id, key, a['url'], 'skipped')
                    return 'skipped'
            alias = db.execute('SELECT article_id FROM article_urls WHERE url=?', (a['url'],)).fetchone()
            if alias is not None and alias[0] != key:
                raise ValueError('已收录的文章链接指向不同的图文，请核实。')
            inserted = db.execute('''INSERT INTO articles(id,account_id,biz,mid,idx,author,url,title,published,body)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(biz,mid,idx) DO NOTHING''',
                (key, job['account_id'], a['biz'], a['mid'], a['idx'], a['author'], a['url'],
                 a['title'], a['published'], a['body']))
            db.execute('INSERT OR IGNORE INTO article_urls(url,article_id) VALUES(?,?)', (a['url'], key))
            status = 'saved' if inserted.rowcount else 'skipped'
            self._mark(db, job_id, key, a['url'], status)
            return status

    def list_articles(self, account_id=None, limit=50, offset=0, q='', sort='newest'):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 2**63-1:
            raise ValueError('文章分页参数无效。')
        if not isinstance(q, str) or (q.strip() and not valid_text(q, 256)):
            raise ValueError('请输入不超过 256 个字符的文章标题或作者。')
        order = {'newest': 'a.published DESC,a.mid DESC,a.idx,a.id',
                 'oldest': 'a.published,a.mid,a.idx,a.id',
                 'title': 'a.title COLLATE NOCASE,a.published DESC,a.id'}
        if sort not in order:
            raise ValueError('文章排序方式无效。')
        filters, args = [], []
        if account_id is not None:
            filters.append('a.account_id=?')
            args.append(account_id)
        if q.strip():
            filters.append('(instr(casefold(a.title),?)>0 OR instr(casefold(a.author),?)>0)')
            args.extend([q.strip().casefold()] * 2)
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        with self.db() as db:
            if q.strip():
                db.create_function('casefold', 1, str.casefold, deterministic=True)
            total = db.execute('SELECT count(*) FROM articles a' + where, args).fetchone()[0]
            rows = db.execute('SELECT ' + ARTICLE_FIELDS + ' FROM articles a' + where +
                              ' ORDER BY ' + order[sort] + ' LIMIT ? OFFSET ?',
                              (*args, limit, offset))
            return dict(articles=[dict(row) for row in rows], total=total)

    def article(self, article_id):
        with self.db() as db:
            row = db.execute('SELECT ' + ARTICLE_FIELDS + ',a.body FROM articles a WHERE a.id=?',
                             (article_id,)).fetchone()
            if row is None:
                raise KeyError(article_id)
            return dict(row)
