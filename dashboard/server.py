#!/usr/bin/env python3
"""Local dashboard for the WeChat saver. Stdlib only; reads the existing recording index."""
import fcntl
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


ROOT = Path('/Volumes/ssd1/wechat-monitor')
KEY = r'[0-9a-f]{64}'
LOCAL_HOST = re.compile(r'(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?', re.I)
RANGE = re.compile(r'bytes=([0-9]*)-([0-9]*)')


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

    def add_author(self, value):
        if not isinstance(value, str) or any(unicodedata.category(c) in ('Cc', 'Zl', 'Zp') for c in value):
            raise ValueError('请输入单行视频号名称。')
        name = value.strip()
        if not name or len(name) > 64 or name.startswith('#'):
            raise ValueError('请输入 1 至 64 个字符的视频号名称，首字符不能为 #。')
        self.authors.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.authors.with_name(self.authors.name + '.lock')
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            text = self.author_text()
            authors = self.parse_authors(text)
            if name in authors:
                return authors, False
            mode = stat.S_IMODE(self.authors.stat().st_mode) if self.authors.exists() else 0o644
            with tempfile.TemporaryDirectory(dir=self.authors.parent) as directory:
                pending = Path(directory) / 'authors'
                with pending.open('w', encoding='utf-8') as out:
                    if text:
                        out.write(text)
                        if not text.endswith('\n'):
                            out.write('\n')
                    out.write(name + '\n')
                    os.fchmod(out.fileno(), mode)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(pending, self.authors)
            return [*authors, name], True

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
        return dict(authors=self.parse_authors(self.author_text()), videos=videos)


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
        if self.command == 'POST':
            if path != '/api/authors':
                self.json_reply(404, {'error': '接口不存在。'})
                return
            origin = self.headers.get('Origin')
            if ((origin is not None and origin != 'http://' + hosts[0])
                    or self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none')):
                self.json_reply(403, {'error': '请从本机管理页提交。'})
                return
            if self.headers.get_content_type() != 'application/json':
                self.json_reply(415, {'error': '预期 JSON 请求。'})
                return
            length = self.headers.get('Content-Length', '')
            if self.headers.get('Transfer-Encoding') or not re.fullmatch(r'[0-9]{1,4}', length) or not 0 < int(length) <= 4096:
                self.json_reply(400, {'error': '请求长度无效。'})
                return
            try:
                data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                authors, added = lib.add_author(data.get('name') if isinstance(data, dict) else None)
            except (ValueError, RecursionError):
                self.json_reply(400, {'error': '请输入有效的单行视频号名称（1 至 64 个字符，首字符不能为 #）。'})
                return
            self.json_reply(201 if added else 200, dict(authors=authors, added=added))
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

    do_GET = do_HEAD = do_POST = respond


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, address, library):
        super().__init__(address, DashboardHandler)
        self.library = library


if __name__ == '__main__':
    address = (os.getenv('DASHBOARD_HOST', '127.0.0.1'), int(os.getenv('DASHBOARD_PORT', '33023')))
    with DashboardServer(address, Library.configured()) as server:
        server.serve_forever()
