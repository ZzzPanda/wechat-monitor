"""Durable jobs for analysis of published video recordings."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    work_key TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size > 0),
    pipeline_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'preparing',
    completed INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL,
    lease_token TEXT,
    lease_until INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(work_key, sha256, pipeline_version)
);
CREATE INDEX IF NOT EXISTS analysis_jobs_ready ON jobs(pipeline_version, status, available_at);
CREATE INDEX IF NOT EXISTS analysis_jobs_leased ON jobs(pipeline_version, status, lease_until);
"""
KEY = re.compile(r'[0-9a-f]{64}')
STAGES = ('preparing', 'analyzing', 'summarizing')


class LeaseError(ValueError):
    """The operation cannot modify a job no longer held by this worker."""


def job_dict(row, *, claimed=False):
    if row is None:
        return None
    job = dict(row)
    if not claimed:
        job.pop('lease_token')
    job.pop('lease_until')
    if job['result'] is not None:
        job['result'] = json.loads(job['result'])
    return job


def public_job(job):
    if job is None:
        return None
    return {key: value for key, value in job.items() if key not in ('path', 'lease_token')}


class Queue:
    def __init__(self, path, *, pipeline_version='1', lease_seconds=600,
                 max_attempts=3, retry_seconds=15):
        self.path = Path(path)
        self.version = pipeline_version
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds
        self._ready = False
        self._init_lock = threading.Lock()

    @contextmanager
    def _db(self, *, write=False):
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            if not self._ready:
                with self._init_lock:
                    if not self._ready:
                        db.executescript(SCHEMA)
                        self._ready = True
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db

    def _by_id(self, db, job_id):
        return db.execute('SELECT * FROM jobs WHERE id=? AND pipeline_version=?',
                          (job_id, self.version)).fetchone()

    def _by_recording(self, db, work_key, sha256):
        return db.execute('SELECT * FROM jobs WHERE work_key=? AND sha256=? AND pipeline_version=?',
                          (work_key, sha256, self.version)).fetchone()

    @contextmanager
    def _lease(self, job_id, lease_token):
        if not isinstance(job_id, str) or not isinstance(lease_token, str):
            raise ValueError('分析任务与租约标识无效。')
        with self._db(write=True) as db:
            row = self._by_id(db, job_id)
            if row is None:
                raise KeyError(job_id)
            now = int(time.time())
            if (row['status'] != 'running' or row['lease_token'] != lease_token
                    or row['lease_until'] <= now):
                raise LeaseError('分析任务租约已失效。')
            yield db, row, now

    def enqueue(self, recording):
        work_key, path, sha256, size = (recording[field] for field in ('work_key', 'path', 'sha256', 'size'))
        if (not all(isinstance(key, str) and KEY.fullmatch(key) for key in (work_key, sha256))
                or not isinstance(path, str) or not path or '\0' in path
                or type(size) is not int or not 0 < size <= 2**63 - 1):
            raise ValueError('录像索引无效。')
        with self._db(write=True) as db:
            now = int(time.time())
            db.execute("""
                INSERT INTO jobs(id,work_key,path,sha256,size,pipeline_version,available_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(work_key,sha256,pipeline_version) DO UPDATE SET
                    path=excluded.path, size=excluded.size, status='queued', stage='preparing',
                    completed=0, total=0, result=NULL, error='', attempts=0,
                    available_at=excluded.available_at, updated_at=excluded.updated_at
                WHERE jobs.status='failed'
            """, (uuid.uuid4().hex, work_key, path, sha256, size, self.version, now, now, now))
            db.execute("""
                UPDATE jobs SET path=?, size=?, updated_at=?
                WHERE work_key=? AND sha256=? AND pipeline_version=? AND status IN ('queued','retry_wait')
                    AND (path<>? OR size<>?)
            """, (path, size, now, work_key, sha256, self.version, path, size))
            return job_dict(self._by_recording(db, work_key, sha256))

    def get(self, work_key, sha256):
        if not self.path.is_file():
            return None
        with self._db() as db:
            return job_dict(self._by_recording(db, work_key, sha256))

    def claim(self):
        with self._db(write=True) as db:
            now = int(time.time())
            db.execute("""
                UPDATE jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                    error='分析进程租约已过期。', available_at=?, lease_token=NULL, lease_until=NULL, updated_at=?
                WHERE pipeline_version=? AND status='running' AND lease_until<=?
            """, (self.max_attempts, now, now, self.version, now))
            row = db.execute("""
                SELECT id FROM jobs WHERE pipeline_version=? AND status IN ('queued','retry_wait') AND available_at<=?
                ORDER BY available_at,rowid LIMIT 1
            """, (self.version, now)).fetchone()
            if row is None:
                return None
            job_id = row['id']
            db.execute("""
                UPDATE jobs SET status='running', stage='preparing', completed=0, total=0, error='',
                    attempts=attempts+1, lease_token=?, lease_until=?, updated_at=? WHERE id=?
            """, (uuid.uuid4().hex, now + self.lease_seconds, now, job_id))
            return job_dict(self._by_id(db, job_id), claimed=True)

    def heartbeat(self, job_id, lease_token):
        with self._lease(job_id, lease_token) as (db, _, now):
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (now + self.lease_seconds, job_id))
            return job_dict(self._by_id(db, job_id))

    def update(self, job_id, lease_token, stage, completed, total):
        if (stage not in STAGES or type(completed) is not int or type(total) is not int
                or not 0 <= completed <= total <= 2**31 - 1):
            raise ValueError('分析任务进度无效。')
        with self._lease(job_id, lease_token) as (db, _, now):
            db.execute("""
                UPDATE jobs SET stage=?, completed=?, total=?, lease_until=?, updated_at=? WHERE id=?
            """, (stage, completed, total, now + self.lease_seconds, now, job_id))
            return job_dict(self._by_id(db, job_id))

    def finish(self, job_id, lease_token, result):
        if not isinstance(result, dict):
            raise ValueError('分析结果必须是 JSON 对象。')
        try:
            data = json.dumps(result, ensure_ascii=False, allow_nan=False)
            data.encode('utf-8')
        except (TypeError, ValueError) as exc:
            raise ValueError('分析结果包含无效 JSON 数据。') from exc
        with self._lease(job_id, lease_token) as (db, _, now):
            db.execute("""
                UPDATE jobs SET status='succeeded', stage='summarizing', completed=total, result=?, error='',
                    lease_token=NULL, lease_until=NULL, updated_at=? WHERE id=?
            """, (data, now, job_id))
            return job_dict(self._by_id(db, job_id))

    def fail(self, job_id, lease_token, error, retryable):
        if not isinstance(error, str) or type(retryable) is not bool:
            raise ValueError('分析任务错误信息无效。')
        error = error.strip()[:500] or '分析任务失败。'
        error.encode('utf-8')
        with self._lease(job_id, lease_token) as (db, row, now):
            status, available_at = 'failed', now
            if retryable and row['attempts'] < self.max_attempts:
                status = 'retry_wait'
                available_at += self.retry_seconds * 2**(row['attempts'] - 1)
            db.execute("""
                UPDATE jobs SET status=?, error=?, available_at=?, lease_token=NULL, lease_until=NULL,
                    updated_at=? WHERE id=?
            """, (status, error, available_at, now, job_id))
            return job_dict(self._by_id(db, job_id))
