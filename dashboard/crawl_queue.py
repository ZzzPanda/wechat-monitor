"""Durable scheduling and progress for the single WeChat collection worker."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), enabled INTEGER NOT NULL,
    interval_minutes INTEGER NOT NULL, next_run INTEGER NOT NULL, start_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, author TEXT NOT NULL, profile_url TEXT, mode TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_by_profile ON jobs(profile_url, status);
CREATE TABLE IF NOT EXISTS history_checkpoints (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    cursor TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS clear_completed_history_checkpoint AFTER UPDATE OF status ON jobs
WHEN NEW.status IN ('done','partial','failed','cancelled')
BEGIN
    DELETE FROM history_checkpoints WHERE job_id=NEW.id;
END;
CREATE TABLE IF NOT EXISTS items (
    job_id TEXT NOT NULL REFERENCES jobs(id), work_key TEXT NOT NULL, url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(job_id, work_key)
);
CREATE TABLE IF NOT EXISTS links (
    job_id TEXT NOT NULL, url TEXT NOT NULL, work_key TEXT NOT NULL,
    PRIMARY KEY(job_id, url), FOREIGN KEY(job_id, work_key) REFERENCES items(job_id, work_key)
);
"""
JOB_SELECT = """
SELECT j.*,
       count(i.work_key) AS discovered,
       count(CASE WHEN i.status='saved' THEN 1 END) AS saved,
       count(CASE WHEN i.status IN ('skipped','baseline') THEN 1 END) AS skipped,
       count(CASE WHEN i.status='failed' THEN 1 END) AS failed,
       (SELECT error FROM items WHERE job_id=j.id AND status='failed' ORDER BY rowid DESC LIMIT 1) AS item_error
FROM jobs j LEFT JOIN items i ON i.job_id=j.id
"""
SUCCESS = ('saved', 'skipped', 'baseline')
QUEUED_INCREMENTAL = "SELECT 1 FROM jobs WHERE status='queued' AND mode='incremental' LIMIT 1"
LAST_UNIX_SECOND = 253402300799  # 9999-12-31 23:59:59 UTC


def settings_row(db):
    row = dict(db.execute('SELECT enabled, interval_minutes, next_run, start_at FROM settings').fetchone())
    row['enabled'] = bool(row['enabled'])
    return row


def next_slot(start_at, interval_minutes, earliest):
    """The first slot on the user's interval grid at or after earliest."""
    step = interval_minutes * 60
    intervals = max(0, earliest - start_at + step - 1) // step
    due = start_at + intervals * step
    if due > LAST_UNIX_SECOND:
        raise ValueError('下一次运行时间超出了可表示的日历范围。')
    return due


def job_row(db, job_id):
    row = db.execute(JOB_SELECT + ' WHERE j.id=? GROUP BY j.id', (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return dict(row)


def job_status(db, job_id):
    row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return row['status']


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self._ready = False
        self._init_lock = threading.Lock()

    @contextmanager
    def db(self, write=False):
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=8)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            if not self._ready:
                with self._init_lock:
                    if not self._ready:
                        db.executescript(SCHEMA)
                        db.execute('BEGIN IMMEDIATE')
                        if 'start_at' not in {r['name'] for r in db.execute('PRAGMA table_info(settings)')}:
                            db.execute('ALTER TABLE settings ADD COLUMN start_at INTEGER')
                            db.execute('UPDATE settings SET start_at=next_run,interval_minutes=max(60,interval_minutes)')
                        first = int(time.time()) + 3600
                        db.execute('INSERT OR IGNORE INTO settings(singleton,enabled,interval_minutes,next_run,start_at) '
                                   'VALUES(1,1,60,?,?)', (first, first))
                        db.commit()
                        self._ready = True
            with db:
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield db

    def snapshot(self):
        with self.db() as db:
            # A long-running history scan can outlive many scheduled rounds.
            # Keep every active job alongside the latest 50 finished jobs.
            rows = db.execute(JOB_SELECT + """
                WHERE j.status IN ('running','queued') OR j.id IN (
                    SELECT id FROM jobs WHERE status NOT IN ('running','queued')
                    ORDER BY created_at DESC,rowid DESC LIMIT 50
                )
                GROUP BY j.id
                ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                         j.created_at DESC,j.rowid DESC""")
            return dict(settings=settings_row(db), jobs=[dict(r) for r in rows])

    def log_entries(self, start, end, limit):
        """Summarize existing completed jobs without exposing command output or share links."""
        modes = {'history': '历史采集', 'incremental': '定时增量'}
        outcomes = {'done': ('完成', 'info'), 'partial': ('部分完成', 'warning'),
                    'cancelled': ('已取消', 'warning'), 'failed': ('失败', 'error')}
        with self.db() as db:
            rows = db.execute("""
                WITH recent AS (
                    SELECT rowid AS ordinal, id, author, mode, status, updated_at FROM jobs
                    WHERE updated_at BETWEEN ? AND ? AND status IN ('done','partial','cancelled','failed')
                    ORDER BY updated_at DESC, rowid DESC LIMIT ?
                )
                SELECT r.author, r.mode, r.status, r.updated_at,
                       count(i.work_key) AS discovered,
                       count(CASE WHEN i.status='saved' THEN 1 END) AS saved,
                       count(CASE WHEN i.status='failed' THEN 1 END) AS failed
                FROM recent r LEFT JOIN items i ON r.id=i.job_id
                GROUP BY r.id ORDER BY r.updated_at DESC, r.ordinal DESC
            """, (start, end, limit)).fetchall()
        entries = []
        for row in rows:
            outcome, level = outcomes[row['status']]
            author = ' '.join(row['author'].split())[:150]
            message = (f'{author} · {modes.get(row["mode"], "视频采集")}{outcome}。'
                       f'发现 {row["discovered"]} 条，保存 {row["saved"]} 条，失败 {row["failed"]} 条。')
            entries.append(dict(time=row['updated_at'], category='crawl', level=level, message=message))
        return entries

    def configure(self, enabled, interval_minutes, start_at=None):
        if type(enabled) is not bool or type(interval_minutes) is not int or interval_minutes < 60:
            raise ValueError('采集间隔须为整数分钟，最短 60 分钟。')
        if start_at is not None and (type(start_at) is not int or not 0 <= start_at <= LAST_UNIX_SECOND):
            raise ValueError('请输入有效的首次运行时间。')
        with self.db(write=True) as db:
            previous = settings_row(db)
            if start_at is None:
                start_at = previous['start_at']
            if (enabled, interval_minutes, start_at) != (previous['enabled'], previous['interval_minutes'], previous['start_at']):
                due = next_slot(start_at, interval_minutes, int(time.time()))
                # A periodic schedule needs a representable following occurrence too.
                next_slot(start_at, interval_minutes, due + 1)
                db.execute('UPDATE settings SET enabled=?,interval_minutes=?,start_at=?,next_run=?',
                           (enabled, interval_minutes, start_at, due))
            return settings_row(db)

    @staticmethod
    def _enqueue(db, entry, mode, now):
        if mode not in ('history', 'incremental'):
            raise ValueError('未知的采集模式。')
        author, url = entry['name'], entry.get('url')
        active = db.execute("""SELECT id FROM jobs WHERE author=? AND profile_url IS ? AND mode=?
                               AND status IN ('queued','running')""", (author, url, mode)).fetchone()
        if active:
            return active['id']
        job_id = uuid.uuid4().hex
        status, error = ('queued', '') if url else ('failed', '请先在监控名单中关联真实的视频号主页。')
        db.execute('INSERT INTO jobs(id,author,profile_url,mode,status,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                   (job_id, author, url, mode, status, error, now, now))
        return job_id

    def enqueue(self, entry, mode='history'):
        with self.db(write=True) as db:
            return job_row(db, self._enqueue(db, entry, mode, int(time.time())))

    def schedule(self, entries, now=None):
        now = int(time.time() if now is None else now)
        with self.db(write=True) as db:
            return self._schedule_due(db, entries, now)

    def _schedule_due(self, db, entries, now):
        config = settings_row(db)
        if not config['enabled'] or now < config['next_run']:
            return []
        job_ids = [self._enqueue(db, entry, 'incremental', now) for entry in entries]
        due = next_slot(config['start_at'], config['interval_minutes'], now + 1)
        db.execute('UPDATE settings SET next_run=?', (due,))
        return job_ids

    def recover(self):
        with self.db(write=True) as db:
            return db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'",
                              (int(time.time()),)).rowcount

    def has_queued(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone())

    def next_queued(self):
        with self.db() as db:
            row = db.execute("""SELECT mode,created_at FROM jobs WHERE status='queued'
                                ORDER BY CASE WHEN mode='incremental' THEN 0 ELSE 1 END,created_at,rowid
                                LIMIT 1""").fetchone()
            return dict(row) if row else None

    def is_running(self, job_id):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM jobs WHERE id=? AND status='running'", (job_id,)).fetchone())

    def activity(self, job_id, author, profile_url, entries, boundary=False):
        if not any(e['name'] == author and e['url'] == profile_url for e in entries):
            return dict(active=False, yield_requested=False)
        with self.db(write=boundary) as db:
            row = db.execute("""SELECT mode FROM jobs WHERE id=? AND author=? AND profile_url=?
                                AND status='running'""", (job_id, author, profile_url)).fetchone()
            if row is None:
                return dict(active=False, yield_requested=False)
            if boundary:
                # Enqueue a slot that fell due between heartbeats before the next video starts.
                self._schedule_due(db, entries, int(time.time()))
            waiting = row['mode'] == 'history' and bool(db.execute(QUEUED_INCREMENTAL).fetchone())
            return dict(active=True, yield_requested=waiting)

    def yield_history(self, job_id, checkpoint, external_incremental=False):
        if not isinstance(checkpoint, dict):
            raise ValueError('历史任务的断点格式无效。')
        cursor = json.dumps(checkpoint, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        with self.db(write=True) as db:
            moved = db.execute(f"""UPDATE jobs SET status='queued',error=?,updated_at=?
                                   WHERE id=? AND mode='history' AND status='running'
                                   AND (? OR EXISTS ({QUEUED_INCREMENTAL}))""",
                               ('等待定时增量完成后继续', int(time.time()), job_id, external_incremental))
            if not moved.rowcount:
                return False
            db.execute("""INSERT INTO history_checkpoints(job_id,cursor) VALUES(?,?)
                          ON CONFLICT(job_id) DO UPDATE SET cursor=excluded.cursor""", (job_id, cursor))
            return True

    def cancel(self, job_id):
        with self.db(write=True) as db:
            db.execute("""UPDATE jobs SET status='cancelled',error=?,updated_at=?
                          WHERE id=? AND status IN ('queued','running')""",
                       ('已取消采集。', int(time.time()), job_id))
            return job_row(db, job_id)

    def claim(self, entries):
        tracked = {(entry['name'], entry.get('url')) for entry in entries}
        now = int(time.time())
        with self.db(write=True) as db:
            self._schedule_due(db, entries, now)
            if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                return None
            rows = db.execute("""SELECT id,author,profile_url FROM jobs WHERE status='queued'
                                 ORDER BY CASE WHEN mode='incremental' THEN 0 ELSE 1 END,created_at,rowid""").fetchall()
            for row in rows:
                job_id = row['id']
                if not row['profile_url'] or (row['author'], row['profile_url']) not in tracked:
                    db.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=? WHERE id=?",
                               ('监控名单中的账号已移除或更换。', now, job_id))
                    continue
                db.execute("UPDATE jobs SET status='running',error='',updated_at=? WHERE id=?", (now, job_id))
                job = job_row(db, job_id)
                if job['mode'] == 'history':
                    saved = db.execute('SELECT cursor FROM history_checkpoints WHERE job_id=?', (job_id,)).fetchone()
                    job['checkpoint'] = json.loads(saved['cursor']) if saved else None
                return job
            return None

    def record(self, job_id, url, key):
        if not all(isinstance(s, str) and s and len(s) <= 4096 for s in (url, key)):
            raise ValueError('需要作品分享链接和作品标识。')
        with self.db(write=True) as db:
            if job_status(db, job_id) != 'running':
                raise ValueError('该采集任务未在运行。')
            previous = db.execute("""SELECT i.work_key,i.status FROM links l JOIN items i USING(job_id,work_key)
                                     WHERE l.job_id=? AND l.url=?""", (job_id, url)).fetchone()
            resolving = previous is not None and previous['work_key'] != key
            if resolving:
                placeholder = 'unresolved:' + hashlib.sha256(url.encode()).hexdigest()
                if previous['work_key'] != placeholder or previous['status'] in SUCCESS or key.startswith('unresolved:'):
                    raise ValueError('分享链接的作品标识发生变化。')
            db.execute('INSERT OR IGNORE INTO items(job_id,work_key,url) VALUES(?,?,?)', (job_id, key, url))
            if resolving:
                db.execute('UPDATE links SET work_key=? WHERE job_id=? AND work_key=?', (key, job_id, placeholder))
                db.execute('DELETE FROM items WHERE job_id=? AND work_key=?', (job_id, placeholder))
            db.execute('INSERT OR IGNORE INTO links(job_id,url,work_key) VALUES(?,?,?)', (job_id, url, key))
            db.execute('UPDATE jobs SET updated_at=? WHERE id=?', (int(time.time()), job_id))
            return dict(db.execute('SELECT url,work_key AS key,status FROM items WHERE job_id=? AND work_key=?',
                                   (job_id, key)).fetchone())

    def item_done(self, job_id, url, status, error=''):
        if status not in (*SUCCESS, 'failed'):
            raise ValueError('未知的作品处理状态。')
        with self.db(write=True) as db:
            row = db.execute("""SELECT i.work_key,i.status FROM links l JOIN items i USING(job_id,work_key)
                                WHERE l.job_id=? AND l.url=?""", (job_id, url)).fetchone()
            if row is None:
                raise KeyError(url)
            if row['status'] in SUCCESS:
                return
            current = job_status(db, job_id)
            if current == 'cancelled' and status not in ('saved', 'skipped'):
                return
            if current not in ('running', 'cancelled'):
                raise ValueError('该采集任务未在运行。')
            db.execute('UPDATE items SET status=?,error=? WHERE job_id=? AND work_key=?',
                       (status, error, job_id, row['work_key']))
            db.execute('UPDATE jobs SET updated_at=? WHERE id=?', (int(time.time()), job_id))

    def finish(self, job_id, status, error=''):
        if status not in ('done', 'partial', 'failed', 'cancelled'):
            raise ValueError('未知的采集完成状态。')
        with self.db(write=True) as db:
            current = job_status(db, job_id)
            if current in (status, 'cancelled'):
                return job_row(db, job_id)
            if current != 'running':
                raise ValueError('该采集任务未在运行。')
            if status != 'cancelled':
                counts = dict(db.execute('SELECT status,count(*) FROM items WHERE job_id=? GROUP BY status',
                                         (job_id,)).fetchall())
                failed, pending = counts.get('failed', 0), counts.get('pending', 0)
                if ((status == 'done' and (failed or pending)) or
                        (status == 'failed' and any(counts.get(s) for s in SUCCESS))):
                    status = 'partial'
                if failed:
                    error = '；'.join(filter(None, (error, f'{failed} 条作品处理失败，可重新拉取历史作品重试。')))
                if pending:
                    error = '；'.join(filter(None, (error, f'{pending} 条作品尚未处理完成。')))
            db.execute('UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?',
                       (status, error, int(time.time()), job_id))
            return job_row(db, job_id)

    def known(self, profile_url, key):
        with self.db() as db:
            complete = db.execute("""SELECT 1 FROM jobs j JOIN items i ON i.job_id=j.id
                        WHERE j.profile_url=? AND j.status='done' AND i.work_key=?
                          AND i.status IN ('saved','baseline','skipped') LIMIT 1""", (profile_url, key)).fetchone()
            if complete:
                return True
            baseline = self.first_partial_baseline(db, profile_url)
            return bool(baseline and db.execute("""SELECT 1 FROM items
                        WHERE job_id=? AND work_key=? AND status='baseline'""", (baseline['id'], key)).fetchone())

    @staticmethod
    def first_partial_baseline(db, profile_url):
        # Older queues can contain several incomplete attempts. Later baseline
        # samples may include new uploads which must remain eligible to download.
        return db.execute("""SELECT j.id,j.created_at FROM jobs j WHERE j.profile_url=? AND j.mode='incremental'
                             AND j.status='partial' AND EXISTS
                             (SELECT 1 FROM items i WHERE i.job_id=j.id AND i.status='baseline')
                             ORDER BY j.created_at,j.rowid LIMIT 1""", (profile_url,)).fetchone()

    def initial_baseline_at(self, profile_url):
        with self.db() as db:
            row = self.first_partial_baseline(db, profile_url)
            return row['created_at'] if row else None

    def last_complete_at(self, profile_url):
        """Use the start of the last successful scan, including works published during it."""
        with self.db() as db:
            row = db.execute("""SELECT created_at FROM jobs WHERE profile_url=? AND status='done'
                                ORDER BY updated_at DESC,rowid DESC LIMIT 1""", (profile_url,)).fetchone()
            return row['created_at'] if row else None

    def seen_before(self, profile_url):
        return self.last_complete_at(profile_url) is not None or self.initial_baseline_at(profile_url) is not None
