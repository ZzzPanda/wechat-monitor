#!/usr/bin/env python3
"""Local dashboard for the WeChat saver. Stdlib only; reads the existing recording index."""
import fcntl
import hmac
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

import public_routes
import video_analysis_routes
from public_articles import PublicArticles
from crawl_queue import LAST_UNIX_SECOND, Queue
from account_search import AccountSelections, DesktopSearch, SearchUnavailable, profile_record, valid_text
from video_analysis_queue import Queue as VideoAnalysisQueue

# The deployed dashboard has a copy of this module next to server.py.
sys.path.append(str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_events import CATEGORIES, RESULT_LIMIT, read_events, write_event
from wechat_settings import (
    DEFAULT_STORAGE_PATH, SETTINGS_FILENAME, STORAGE_ROOT, SettingsError, SettingsStore, indexed_path,
)


ROOT = STORAGE_ROOT.parent
KEY = r'[0-9a-f]{64}'
LOCAL_HOST = re.compile(r'(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?', re.I)
RANGE = re.compile(r'bytes=([0-9]*)-([0-9]*)')


PROFILE_PREFIX = '# wechat-profile '


class IdentityConflict(Exception):
    pass


class AuthorList:
    """One snapshot of the saver-compatible names and their dashboard comments."""

    def __init__(self, text):
        self.text = text
        self.lines = []
        names = {}
        metadata = {}
        for raw in text.splitlines(keepends=True):
            value = raw.strip()
            is_name = bool(value and not value.startswith('#'))
            owner = None
            if is_name:
                owner = value
                names[owner] = None
            elif raw.startswith(PROFILE_PREFIX):
                try:
                    item = json.loads(raw[len(PROFILE_PREFIX):])
                except (ValueError, RecursionError):
                    item = None
                if isinstance(item, dict) and isinstance(item.get('name'), str):
                    owner = item['name'].strip()
                    profile = profile_record(item)
                    if profile or 'url' not in item:
                        record = profile or {'name': owner}
                        if valid_text(item.get('note'), 200):
                            record['note'] = item['note'].strip()
                        if isinstance(item.get('revision'), str):
                            record['revision'] = item['revision']
                        metadata[owner] = record
            self.lines.append((owner, is_name, raw))
        self.records = {name: metadata.get(name, {'name': name}) for name in names}

    @staticmethod
    def entry(record):
        # Include the contents so that ordinary file edits invalidate open forms too.
        data = json.dumps(record, sort_keys=True).encode()
        return dict(name=record['name'], url=record.get('url'), note=record.get('note', ''),
                    version=hashlib.sha256(data).hexdigest())

    def current(self, name, version):
        record = self.records[name]
        if version != self.entry(record)['version']:
            raise IdentityConflict('这条监控记录已经变更，请刷新后重试。')
        return record

    def render_change(self, name, record):
        # Replace the first name in place. Remove duplicate names and comments
        # owned by this record, including any orphaned metadata for its new name.
        owners = {name}
        block = ''
        if record is not None:
            owners.add(record['name'])
            block = record['name'] + '\n' + PROFILE_PREFIX + json.dumps(record, ensure_ascii=False) + '\n'
        output = []
        for owner, is_name, raw in self.lines:
            if owner not in owners:
                output.append(raw)
            elif owner == name and is_name and block:
                output.append(block)
                block = ''
        text = ''.join(output)
        if block and text and not text.endswith('\n'):
            text += '\n'
        return text + block


def same_profile(left, right):
    if left.get('id') and right.get('id'):
        return left['id'] == right['id']
    return bool(left.get('url') and left['url'] == right.get('url'))


def saved_profile(account):
    verified = profile_record(account)
    if verified is None:
        raise ValueError('请选择微信搜索返回的视频号主页。')
    return {k: verified[k] for k in ('name', 'url', 'id') if k in verified}


@dataclass
class Library:
    authors: Path
    database: Path
    media: Path
    posters: Path
    static: Path
    media_root: Optional[Path] = None
    storage_root: Path = STORAGE_ROOT

    @classmethod
    def configured(cls):
        media = os.getenv('DASHBOARD_MEDIA')
        media_root = os.getenv('DASHBOARD_MEDIA_ROOT')
        return cls(
            Path(os.getenv('DASHBOARD_AUTHORS', ROOT / 'state/monitoring/authors.txt')),
            Path(os.getenv('DASHBOARD_DB', ROOT / 'state/wechat-pilot/previews.sqlite3')),
            Path(media) if media else DEFAULT_STORAGE_PATH,
            Path(os.getenv('DASHBOARD_POSTERS', ROOT / 'state/posters')),
            Path(__file__).resolve().with_name('static'),
            Path(media_root) if media_root else (None if media else STORAGE_ROOT),
        )

    def author_text(self):
        try:
            return self.authors.read_text(encoding='utf-8')
        except FileNotFoundError:
            return ''

    @staticmethod
    def parse_authors(text):
        return list(dict.fromkeys(line.strip() for line in text.splitlines()
                                  if line.strip() and not line.lstrip().startswith('#')))

    def entries(self):
        snapshot = AuthorList(self.author_text())
        return [snapshot.entry(record) for record in snapshot.records.values()]

    @contextmanager
    def locked_authors(self):
        self.authors.parent.mkdir(parents=True, exist_ok=True)
        with self.authors.with_name(self.authors.name + '.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield AuthorList(self.author_text())

    def write_change(self, snapshot, name, record):
        text = snapshot.render_change(name, record)
        if text == snapshot.text:
            return
        mode = stat.S_IMODE(self.authors.stat().st_mode) if self.authors.exists() else 0o644
        with tempfile.TemporaryDirectory(dir=self.authors.parent) as directory:
            pending = Path(directory) / 'authors'
            with pending.open('w', encoding='utf-8') as out:
                out.write(text)
                os.fchmod(out.fileno(), mode)
                out.flush()
                os.fsync(out.fileno())
            os.replace(pending, self.authors)

    def add_author(self, account):
        saved = saved_profile(account)
        name = saved['name']
        with self.locked_authors() as snapshot:
            authors = list(snapshot.records)
            previous = snapshot.records.get(name, {})
            if previous.get('url'):
                if not same_profile(previous, saved):
                    raise IdentityConflict('名单中已有另一条同名视频号记录，请在原记录中更换账号。')
                self.write_change(snapshot, name, previous)
                return authors, False, False
            if any(same_profile(item, saved) for item in snapshot.records.values()):
                return authors, False, False
            if previous.get('note'):
                saved['note'] = previous['note']
            saved['revision'] = secrets.token_hex(16)
            self.write_change(snapshot, name, saved)
            if previous:
                return authors, False, True
            return [*authors, name], True, False

    def update_author(self, name, version, *, note=None, account=None):
        if note is not None and note != '' and not valid_text(note, 200):
            raise ValueError('备注最多 200 个字符，请使用单行文字。')
        with self.locked_authors() as snapshot:
            previous = snapshot.current(name, version)
            saved = previous.copy()
            if account is not None:
                profile = saved_profile(account)
                for other_name, other in snapshot.records.items():
                    if other_name != name and (other_name == profile['name'] or same_profile(profile, other)
                                               or other.get('url') == profile['url']):
                        raise IdentityConflict('所选视频号或其名称已在监控名单中，请选择其他账号。')
                saved.pop('id', None)
                saved.pop('description', None)
                saved.update(profile)
            if note is not None:
                saved.pop('note', None)
                if note.strip():
                    saved['note'] = note.strip()
            if saved != previous:
                saved['revision'] = secrets.token_hex(16)
            self.write_change(snapshot, name, saved)
            return snapshot.entry(saved)

    def delete_author(self, name, version):
        with self.locked_authors() as snapshot:
            snapshot.current(name, version)
            self.write_change(snapshot, name, None)

    def recordings(self, key=None, *, include_digest=False):
        if not self.database.is_file():
            return []
        with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('works','recordings')").fetchone()[0]
            if tables != 2:
                return []
            digest_column = ''
            if include_digest:
                columns = {column['name'] for column in db.execute('PRAGMA table_info(recordings)')}
                if 'sha256' not in columns:
                    return []
                digest_column = ', r.sha256'
            sql = ('SELECT r.work_key, r.path, r.size, w.author, w.caption, w.published' + digest_column +
                   ' FROM recordings r JOIN works w ON r.work_key = w.fingerprint')
            if key is not None:
                return db.execute(sql + ' WHERE r.work_key=?', (key,)).fetchall()
            return db.execute(sql + ' ORDER BY w.published DESC, r.work_key').fetchall()

    def duration(self, key):
        try:
            metadata = json.loads((self.posters / (key + '.json')).read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        value = metadata.get('duration') if isinstance(metadata, dict) else None
        return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None

    def dashboard(self):
        videos = []
        for row in self.recordings():
            key = row['work_key']
            if not re.fullmatch(KEY, key):
                continue
            title = row['caption'].strip().splitlines()
            poster = self.posters / (key + '.jpg')
            videos.append(dict(id=key, author=row['author'], title=title[0] if title else '未命名作品',
                               published=row['published'], size=row['size'], duration=self.duration(key),
                               url='/media/' + key + '.mp4', poster='/posters/' + key + '.jpg' if poster.is_file() else None))
        snapshot = AuthorList(self.author_text())
        entries = [snapshot.entry(record) for record in snapshot.records.values()]
        profiles = [profile_record(record) for record in snapshot.records.values() if record.get('url')]
        return dict(authors=list(snapshot.records), profiles=profiles, entries=entries, videos=videos)

    def recording_file(self, key):
        rows = self.recordings(key)
        path = self.resolve_recording_path(rows[0]['path']) if rows else None
        return path if path is not None and path.is_file() else None

    def resolve_recording_path(self, value):
        if not isinstance(value, str) or (self.media_root is None and value.startswith('./')):
            return None
        try:
            return indexed_path(self.media_root or self.media, self.media, value)
        except ValueError:
            return None

    def analysis_recording(self, key):
        rows = self.recordings(key, include_digest=True)
        if not rows:
            return None
        row = rows[0]
        path = self.resolve_recording_path(row['path'])
        digest, size = row['sha256'], row['size']
        if path is None or not isinstance(digest, str) or not re.fullmatch(KEY, digest):
            return None
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if type(size) is not int or size <= 0 or info.st_size != size or not stat.S_ISREG(info.st_mode):
            return None
        return {field: row[field] for field in ('work_key', 'path', 'sha256', 'size')}


def local_file(root, relative):
    """Resolve both the stored path and any symlinks inside the mounted directory."""
    base = root.resolve()
    path = (base / relative).resolve()
    return path if path.is_relative_to(base) and path.is_file() else None


def byte_range(header, size):
    match = RANGE.fullmatch(header)
    if not match or not any(match.groups()) or size == 0:
        raise ValueError('invalid range')
    first, last = match.groups()
    if not first:
        count = int(last)
        if count == 0:
            raise ValueError('invalid suffix')
        return max(0, size - count), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start > end:
        raise ValueError('unsatisfiable range')
    return start, end


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'WeChatDashboard/1'

    def log_request(self, code='-', size='-'):
        # Queue progress lives in SQLite. Keep the periodic heartbeat out of Docker logs.
        if code == 200 and self.command == 'POST' and self.path in (
                '/api/crawl-worker', video_analysis_routes.WORKER_PATH):
            return
        super().log_request(code, size)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        super().end_headers()

    def json_reply(self, code, content):
        data = json.dumps(content, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def serve_file(self, path, media_type, seekable=False):
        if path is None:
            self.json_reply(404, {'error': '文件不存在。'})
            return
        with path.open('rb') as src:
            size = os.fstat(src.fileno()).st_size
            start, end = 0, size - 1
            requested = self.headers.get('Range') if seekable else None
            if requested is not None:
                try:
                    start, end = byte_range(requested, size)
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            self.send_response(206 if requested is not None else 200)
            self.send_header('Content-Type', media_type)
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Cache-Control', 'private, no-cache')
            if seekable:
                self.send_header('Accept-Ranges', 'bytes')
            if requested is not None:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command != 'HEAD':
                src.seek(start)
                left = end - start + 1
                while left:
                    chunk = src.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

    def read_json(self, max_length=4096):
        if self.headers.get_content_type() != 'application/json':
            self.json_reply(415, {'error': '预期 JSON 请求。'})
            return
        length = self.headers.get('Content-Length', '')
        if (self.headers.get('Transfer-Encoding') or not length.isascii() or not length.isdecimal()
                or len(length) > len(str(max_length)) or not 0 < int(length) <= max_length):
            self.json_reply(400, {'error': '请求长度无效。'})
            return
        try:
            data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, RecursionError):
            self.json_reply(400, {'error': '请求格式无效。'})

    def mutate(self, path, data):
        lib = self.server.library
        queue = self.server.crawls
        method = self.command
        if method == 'POST' and path == '/api/authors':
            if set(data) != {'token'}:
                raise ValueError('请先搜索，再选择要添加的视频号。')
            authors, added, linked = lib.add_author(self.server.selections.resolve(data['token']))
            self.json_reply(201 if added else 200, dict(authors=authors, added=added, linked=linked))
        elif method in ('PATCH', 'DELETE') and (path == '/api/authors' or path.startswith('/api/authors/')):
            name = path[len('/api/authors/'):]
            if path == '/api/authors':
                names = parse_qs(urlsplit(self.path).query, max_num_fields=1).get('name', [])
                name = names[0] if len(names) == 1 else ''
            version = data.get('version')
            if not name or not valid_text(version, 256):
                raise ValueError('请刷新名单后重试。')
            if method == 'DELETE':
                if set(data) != {'version'}:
                    raise ValueError('移除请求格式无效。')
                lib.delete_author(name, version)
                self.json_reply(200, {'removed': True})
            else:
                fields = data.keys() - {'version'}
                if not fields or fields - {'token', 'note'}:
                    raise ValueError('可修改备注，或从搜索结果中选择新的视频号。')
                if 'note' in fields and not isinstance(data['note'], str):
                    raise ValueError('备注必须为文本。')
                account = self.server.selections.resolve(data['token']) if 'token' in fields else None
                entry = lib.update_author(name, version, note=data.get('note'), account=account)
                self.json_reply(200, dict(entry=entry))
        elif method == 'PATCH' and path == '/api/crawl-settings':
            if set(data) not in ({'enabled', 'interval_minutes'}, {'enabled', 'interval_minutes', 'start_at'}):
                raise ValueError('请提供定时开关、采集间隔和首次运行时间。')
            if 'start_at' in data and type(data['start_at']) is not int:
                raise ValueError('首次运行时间无效。')
            settings = queue.configure(data['enabled'], data['interval_minutes'], data.get('start_at'))
            write_event(lib.authors.parent, 'crawl', 'info', '已保存定时采集设置。')
            self.json_reply(200, dict(settings=settings))
        elif method == 'POST' and path == '/api/crawls':
            if set(data) != {'name', 'version'}:
                raise ValueError('请从监控名单中选择账号。')
            entry = next((e for e in lib.entries() if e['name'] == data['name']), None)
            if entry is None:
                raise KeyError
            if entry['version'] != data['version']:
                raise IdentityConflict('账号信息已变更，请刷新后重试。')
            if not entry['url']:
                raise ValueError('请先搜索并关联视频号主页。')
            self.json_reply(202, dict(job=queue.enqueue(entry)))
        elif method == 'POST' and (match := re.fullmatch(r'/api/crawls/([a-f0-9]{32})/cancel', path)):
            if data:
                raise ValueError('停止请求格式无效。')
            self.json_reply(200, dict(job=queue.cancel(match[1])))
        elif method == 'POST' and path == '/api/crawl-worker':
            self.json_reply(200, self.server.worker_call(data))
        else:
            self.json_reply(404, {'error': '接口不存在。'})

    def handle_settings(self):
        store = self.server.settings
        try:
            if self.command in ('GET', 'HEAD'):
                self.json_reply(200, store.public())
            elif self.command == 'PUT':
                data = self.read_json(max_length=32768)
                if data is not None:
                    settings = store.update(data)
                    write_event(self.server.library.authors.parent, 'system', 'info', '已保存管理台设置。')
                    self.json_reply(200, settings)
            else:
                self.json_reply(405, {'error': '此方法不适用于配置接口。'})
        except (OSError, SettingsError):
            self.json_reply(503, {'error': '暂时无法访问配置，请检查外置盘后重试。'})
        except ValueError as err:
            self.json_reply(400, {'error': str(err)})

    def handle_directories(self):
        if self.command not in ('GET', 'HEAD'):
            self.json_reply(405, {'error': '此方法不适用于目录浏览接口。'})
            return
        try:
            query = urlsplit(self.path).query
            params = parse_qs(query, keep_blank_values=True, max_num_fields=1,
                              strict_parsing=True, errors='strict') if query else {}
            if params.keys() - {'path'}:
                raise ValueError
        except ValueError:
            self.json_reply(400, {'error': '目录浏览请求格式无效。'})
            return
        try:
            self.json_reply(200, self.server.settings.directories(params.get('path', [None])[0]))
        except FileNotFoundError:
            self.json_reply(404, {'error': '文件夹不存在，可能已被移动。'})
        except OSError:
            self.json_reply(503, {'error': '暂时无法读取这个文件夹，请检查外置盘或访问权限。'})
        except ValueError as err:
            self.json_reply(400, {'error': str(err)})

    def handle_logs(self):
        if self.command not in ('GET', 'HEAD'):
            self.json_reply(405, {'error': '此方法不适用于日志接口。'})
            return
        try:
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=3)
            if params.keys() - {'category', 'start', 'end'} or any(len(values) != 1 for values in params.values()):
                raise ValueError
            category = params.get('category', ['all'])[0]
            if category != 'all' and category not in CATEGORIES:
                raise ValueError
            if 'start' in params or 'end' in params:
                raw = [params.get(name, [''])[0] for name in ('start', 'end')]
                if not all(re.fullmatch(r'[0-9]{1,12}', value) for value in raw):
                    raise ValueError
                start, end = map(int, raw)
                if start > end or end > LAST_UNIX_SECOND:
                    raise ValueError
            else:
                end = int(time.time())
                start = max(0, end - 86400)
        except ValueError:
            self.json_reply(400, {'error': '请选择有效的日志分区和时间范围。'})
            return
        try:
            limit = RESULT_LIMIT + 1
            entries = read_events(self.server.library.authors.parent, category, start, end, limit)
            if category in ('all', 'crawl'):
                entries.extend(self.server.crawls.log_entries(start, end, limit))
                entries.extend(self.server.public_articles.log_entries(start, end, limit))
                entries.sort(key=lambda entry: entry['time'], reverse=True)
            self.json_reply(200, dict(entries=entries[:RESULT_LIMIT], truncated=len(entries) > RESULT_LIMIT))
        except (OSError, sqlite3.Error) as err:
            self.log_error('Logs unavailable: %s', err)
            self.json_reply(503, {'error': '暂时无法读取日志，请稍后重试。'})

    def handle_site(self):
        hosts = self.headers.get_all('Host') or []
        if len(hosts) != 1 or not LOCAL_HOST.fullmatch(hosts[0]):
            self.json_reply(403, {'error': '请使用本机地址访问管理页。'})
            return
        lib = self.server.library
        try:
            path = unquote(urlsplit(self.path).path)
        except ValueError:
            self.json_reply(400, {'error': '请求路径无效。'})
            return
        if '\x00' in path:
            self.json_reply(400, {'error': '请求路径无效。'})
            return
        analysis = video_analysis_routes.VIDEO_PATH.fullmatch(path)
        analysis_worker = path == video_analysis_routes.WORKER_PATH
        mutating = self.command in ('POST', 'PUT', 'PATCH', 'DELETE')
        if (mutating or analysis or analysis_worker or path.startswith('/api/public')
                or path in ('/api/author-search', '/api/settings', '/api/settings/directories', '/api/logs')):
            origin = self.headers.get('Origin')
            if ((origin is not None and origin != 'http://' + hosts[0])
                    or self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none')):
                self.json_reply(403, {'error': '请从本机管理页提交。'})
                return
        if analysis or analysis_worker:
            video_analysis_routes.handle(self, analysis[1] if analysis else None)
            return
        if path == '/api/settings':
            self.handle_settings()
            return
        if path == '/api/settings/directories':
            self.handle_directories()
            return
        if path == '/api/logs':
            self.handle_logs()
            return
        if path in ('/api/author-search', '/api/public/search'):
            if self.command != 'GET' or self.headers.get('Sec-Fetch-Dest', 'empty') != 'empty':
                self.json_reply(400, {'error': '请在管理页中搜索。'})
                return
            try:
                params = parse_qs(urlsplit(self.path).query, max_num_fields=2, keep_blank_values=True)
            except ValueError:
                params = {}
            query = params.get('q', [])
            if (len(query) != 1 or not valid_text(query[0], 64)
                    or params.keys() - {'q', 'fresh'} or params.get('fresh', ['1']) != ['1']):
                self.json_reply(400, {'error': '请输入 1 至 64 个字符的账号名称。'})
                return
            try:
                selections = self.server.public_selections if path == '/api/public/search' else self.server.selections
                response = selections.find(query[0].strip(), fresh='fresh' in params)
            except SearchUnavailable as err:
                self.json_reply(503, {'error': str(err)})
                return
            self.json_reply(200, response)
            return
        if path == '/api/public' or path.startswith('/api/public/'):
            try:
                reply = public_routes.handle_public(self, path)
                if reply is not None:
                    self.json_reply(*reply)
            except public_routes.AccountConflict as err:
                self.json_reply(409, {'error': str(err)})
            except ValueError as err:
                self.json_reply(400, {'error': str(err)})
            except KeyError:
                self.json_reply(404, {'error': '未找到公众号、文章或任务。'})
            return
        if mutating:
            worker = self.command == 'POST' and path == '/api/crawl-worker'
            if worker and not self.server.worker_authorized(self.headers.get('X-Crawl-Worker', '')):
                self.json_reply(403, {'error': '仅采集进程可访问。'})
                return
            data = self.read_json(max_length=8 * 1024 * 1024 if worker else 4096)
            if data is None:
                return
            try:
                self.mutate(path, data)
            except ValueError as err:
                self.json_reply(400, {'error': str(err)})
            except IdentityConflict as err:
                self.json_reply(409, {'error': str(err)})
            except KeyError:
                self.json_reply(404, {'error': '记录已不存在，请刷新名单。'})
        elif path == '/api/authors':
            self.json_reply(200, dict(entries=lib.entries()))
        elif path == '/api/crawls':
            self.json_reply(200, dict(self.server.crawls.snapshot(),
                                      worker_online=time.monotonic() - self.server.last_tick < 25))
        elif path == '/api/dashboard':
            self.json_reply(200, lib.dashboard())
        elif (match := re.fullmatch(r'/media/(' + KEY + r')\.mp4', path)):
            self.serve_file(lib.recording_file(match[1]), 'video/mp4', seekable=True)
        elif (match := re.fullmatch(r'/posters/(' + KEY + r')\.jpg', path)):
            self.serve_file(local_file(lib.posters, match[1] + '.jpg'), 'image/jpeg')
        else:
            file = local_file(lib.static, 'index.html' if path == '/' else path.lstrip('/'))
            mime = mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
            self.serve_file(file, mime + '; charset=utf-8' if mime.startswith('text/') else mime)

    def respond(self):
        try:
            self.handle_site()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Native players cancel previous requests while seeking.
        except (OSError, sqlite3.Error) as err:
            self.log_error('Dashboard unavailable: %s', err)
            self.json_reply(503, {'error': '暂时无法读取视频库或写入名单，请稍后重试。'})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = respond


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address, library, search=None, public_search=None):
        super().__init__(address, DashboardHandler)
        self.library = library
        self.selections = AccountSelections(search or DesktopSearch(library.authors.parent))
        self.public_selections = AccountSelections(
            public_search or DesktopSearch(library.authors.parent, kind='public'),
            validate=public_routes.public_candidate, label='公众号')
        self.public_articles = PublicArticles(library.authors.parent / 'public-articles.sqlite3')
        self.settings = SettingsStore(library.authors.parent / SETTINGS_FILENAME, library.storage_root,
                                      library.media_root)

        self.last_tick = float('-inf')
        self.crawls = Queue(library.authors.parent / 'crawl.sqlite3')
        self.last_video_analysis_tick = float('-inf')
        self.video_analysis = VideoAnalysisQueue(library.authors.parent / 'video-analysis.sqlite3')

    def worker_authorized(self, supplied, key_name='crawl-worker.key'):
        try:
            secret = (self.library.authors.parent / key_name).read_text().strip()
            return len(secret) >= 32 and secret.isascii() and supplied.isascii() and hmac.compare_digest(secret, supplied)
        except (FileNotFoundError, UnicodeError):
            return False

    def worker_call(self, data):
        queue = self.crawls
        articles = self.public_articles
        op = data.get('op')
        if op == 'tick':
            queue.schedule(self.library.entries())
            articles.schedule()
            self.last_tick = time.monotonic()
            return dict(queued=queue.has_queued() or articles.has_queued())
        elif op == 'claim':
            video = queue.next_queued()
            article = articles.next_queued()
            if article and (not video or (article['mode'] != 'incremental', article['created_at'])
                            < (video['mode'] != 'incremental', video['created_at'])):
                job = articles.claim()
                if job:
                    return dict(job=dict(job, source='public'))
            return dict(job=queue.claim(self.library.entries()))
        elif op == 'active':
            boundary = data.get('boundary', False)
            if type(boundary) is not bool:
                raise ValueError('作品边界标记无效。')
            state = queue.activity(data['id'], data['author'], data['profile_url'], self.library.entries(), boundary)
            if boundary and state['active'] and (articles.next_queued() or {}).get('mode') == 'incremental':
                state['yield_requested'] = True
            return state
        elif op == 'yield_history':
            waiting = (articles.next_queued() or {}).get('mode') == 'incremental'
            return dict(yielded=queue.yield_history(data['id'], data['checkpoint'], external_incremental=waiting))
        elif op == 'recover':
            queue.recover()
            articles.recover()
        elif isinstance(op, str) and op.startswith('public_'):
            return public_routes.worker_call(articles, op[len('public_'):], data, queue)
        elif op == 'seen_before':
            last_complete = queue.last_complete_at(data['profile_url'])
            boundary = last_complete if last_complete is not None else queue.initial_baseline_at(data['profile_url'])
            return dict(known=boundary is not None, since=boundary)
        elif op == 'known':
            return dict(known=queue.known(data['profile_url'], data['key']))
        elif op == 'record':
            return dict(item=queue.record(data['id'], data['url'], data['key']))
        elif op == 'item_done':
            queue.item_done(data['id'], data['url'], data['status'], data.get('error', ''))
        elif op == 'finish':
            queue.finish(data['id'], data['status'], data.get('error', ''))
        else:
            raise ValueError('采集进程操作无效。')
        return {'ok': True}


if __name__ == '__main__':
    address = (os.getenv('DASHBOARD_HOST', '127.0.0.1'), int(os.getenv('DASHBOARD_PORT', '33023')))
    with DashboardServer(address, Library.configured()) as server:
        server.serve_forever()
