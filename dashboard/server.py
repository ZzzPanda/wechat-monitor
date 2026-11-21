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
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from crawl_queue import Queue
from account_search import AccountSelections, DesktopSearch, SearchUnavailable, profile_record, valid_text


ROOT = Path('/Volumes/ssd1/wechat-monitor')
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

    @classmethod
    def configured(cls):
        return cls(
            Path(os.getenv('DASHBOARD_AUTHORS', ROOT / 'state/monitoring/authors.txt')),
            Path(os.getenv('DASHBOARD_DB', ROOT / 'state/wechat-pilot/previews.sqlite3')),
            Path(os.getenv('DASHBOARD_MEDIA', ROOT / 'media/微信视频号')),
            Path(os.getenv('DASHBOARD_POSTERS', ROOT / 'state/posters')),
            Path(__file__).resolve().with_name('static'),
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

    def recordings(self, key=None):
        if not self.database.is_file():
            return []
        sql = ('SELECT r.work_key, r.path, r.size, w.author, w.caption, w.published '
               'FROM recordings r JOIN works w ON r.work_key = w.fingerprint')
        with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('works','recordings')").fetchone()[0]
            if tables != 2:
                return []
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
        if code == 200 and self.command == 'POST' and self.path == '/api/crawl-worker':
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

    def read_json(self):
        if self.headers.get_content_type() != 'application/json':
            self.json_reply(415, {'error': '预期 JSON 请求。'})
            return
        length = self.headers.get('Content-Length', '')
        if (self.headers.get('Transfer-Encoding') or not re.fullmatch(r'[0-9]{1,4}', length)
                or not 0 < int(length) <= 4096):
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
            self.json_reply(200, dict(settings=queue.configure(data['enabled'], data['interval_minutes'],
                                                               data.get('start_at'))))
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
            if not self.server.worker_authorized(self.headers.get('X-Crawl-Worker', '')):
                self.json_reply(403, {'error': '仅采集进程可访问。'})
                return
            self.json_reply(200, self.server.worker_call(data))
        else:
            self.json_reply(404, {'error': '接口不存在。'})

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
        if self.command in ('POST', 'PATCH', 'DELETE') or path == '/api/author-search':
            origin = self.headers.get('Origin')
            if ((origin is not None and origin != 'http://' + hosts[0])
                    or self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none')):
                self.json_reply(403, {'error': '请从本机管理页提交。'})
                return
        if path == '/api/author-search':
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
                self.json_reply(400, {'error': '请输入 1 至 64 个字符的视频号名称。'})
                return
            try:
                response = self.server.selections.find(query[0].strip(), fresh='fresh' in params)
            except SearchUnavailable as err:
                self.json_reply(503, {'error': str(err)})
                return
            self.json_reply(200, response)
            return
        if self.command in ('POST', 'PATCH', 'DELETE'):
            data = self.read_json()
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
            rows = lib.recordings(match[1])
            file = local_file(lib.media, rows[0]['path']) if rows else None
            self.serve_file(file, 'video/mp4', seekable=True)
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

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = respond


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address, library, search=None):
        super().__init__(address, DashboardHandler)
        self.library = library
        self.selections = AccountSelections(search or DesktopSearch(library.authors.parent))

        self.last_tick = float('-inf')
        self.crawls = Queue(library.authors.parent / 'crawl.sqlite3')

    def worker_authorized(self, supplied):
        try:
            secret = (self.library.authors.parent / 'crawl-worker.key').read_text().strip()
            return len(secret) >= 32 and supplied.isascii() and hmac.compare_digest(secret, supplied)
        except FileNotFoundError:
            return False

    def worker_call(self, data):
        queue = self.crawls
        op = data.get('op')
        if op == 'tick':
            queue.schedule(self.library.entries())
            self.last_tick = time.monotonic()
            return dict(queued=queue.has_queued())
        elif op == 'claim':
            return dict(job=queue.claim(self.library.entries()))
        elif op == 'active':
            boundary = data.get('boundary', False)
            if type(boundary) is not bool:
                raise ValueError('作品边界标记无效。')
            return queue.activity(data['id'], data['author'], data['profile_url'], self.library.entries(), boundary)
        elif op == 'yield_history':
            return dict(yielded=queue.yield_history(data['id'], data['checkpoint']))
        elif op == 'recover':
            queue.recover()
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
