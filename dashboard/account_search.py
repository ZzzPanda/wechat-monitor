"""Ask the signed-in WeChat desktop for profiles and scope selections to its replies."""
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import uuid
from urllib.parse import parse_qsl, urlsplit


class SearchUnavailable(Exception):
    pass


def valid_text(value, limit):
    return (isinstance(value, str) and 0 < len(value.strip()) <= limit
            and all(unicodedata.category(c) not in ('Cc', 'Zl', 'Zp', 'Cs') for c in value))


def profile_record(row):
    if not isinstance(row, dict) or not valid_text(row.get('name'), 64):
        return None
    name = row['name'].strip()
    url = row.get('url')
    if name.startswith('#') or not valid_text(url, 2048) or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != 'https' or parts.fragment:
        return None
    result = dict(name=name, url=url)
    if parts.netloc == 'channels.weixin.qq.com' and parts.path == '/web/pages/profile':
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if len(pairs) != 1 or pairs[0][0] != 'username' or not re.fullmatch(r'[A-Za-z0-9_.-]{1,220}@finder', pairs[0][1]):
            return None
        # Read the identity from the actual profile address, never from form fields.
        result['id'] = pairs[0][1]
    elif (parts.netloc == 'weixin.qq.com' and re.fullmatch(r'/f/[A-Za-z0-9_-]+', parts.path)
          and not parts.query):
        if valid_text(row.get('id'), 256):
            result['id'] = row['id']
    else:
        return None
    if valid_text(row.get('description'), 512):
        result['description'] = row['description']
    return result


class DesktopSearch:
    def __init__(self, folder, timeout=115):
        self.folder = Path(folder)
        self.timeout = timeout
        self.lock = threading.Lock()

    def __call__(self, query):
        if not self.lock.acquire(blocking=False):
            raise SearchUnavailable('微信正在执行另一项搜索，请稍后重试。')
        key = uuid.uuid4().hex + '.json'
        request = self.folder / 'search-requests' / key
        reply = self.folder / 'search-replies' / key
        try:
            # Publish only a complete request, in the same mounted filesystem.
            with tempfile.NamedTemporaryFile(dir=self.folder, delete=False, mode='w', encoding='utf-8') as out:
                pending = Path(out.name)
                try:
                    json.dump(dict(q=query, expires=time.time() + self.timeout), out, ensure_ascii=False)
                    out.flush()
                    os.replace(pending, request)
                finally:
                    pending.unlink(missing_ok=True)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    with reply.open(encoding='utf-8') as source:
                        data = json.load(source)
                except FileNotFoundError:
                    time.sleep(0.25)
                    continue
                if not isinstance(data, dict):
                    break
                if valid_text(data.get('error'), 512):
                    raise SearchUnavailable(data['error'])
                if isinstance(data.get('results'), list):
                    return data['results']
                break
            raise SearchUnavailable('微信搜索未在时限内返回结果，请检查桌面登录状态后重试。')
        finally:
            try:
                request.unlink(missing_ok=True)
                reply.unlink(missing_ok=True)
            finally:
                self.lock.release()


class AccountSelections:
    def __init__(self, search):
        self.search = search
        self.lock = threading.Lock()
        self.tokens = {}

    def find(self, query):
        rows = self.search(query)
        results = []
        now = time.monotonic()
        with self.lock:
            self.tokens = {k: v for k, v in self.tokens.items() if v[0] > now}
            seen = set()
            for row in rows[:30]:
                account = profile_record(row)
                if account is None:
                    continue
                identity = account.get('id') or account['url']
                if identity in seen:
                    continue
                seen.add(identity)
                token = secrets.token_urlsafe(32)
                self.tokens[token] = (now + 600, account)
                results.append(dict(account, token=token))
            if rows and not results:
                raise SearchUnavailable('微信返回了候选账号，但未能核实主页地址。请用完整账号名重试。')
            while len(self.tokens) > 1024:
                self.tokens.pop(next(iter(self.tokens)))
        return results

    def resolve(self, token):
        if isinstance(token, str):
            with self.lock:
                record = self.tokens.get(token)
                if record and record[0] > time.monotonic():
                    return record[1]
        raise ValueError('请选择本次搜索返回的视频号。选择已过期时请重新搜索。')
