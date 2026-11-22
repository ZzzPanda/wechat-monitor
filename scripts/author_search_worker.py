"""Run dashboard account searches on the Mac while the WeChat player is available."""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

from wechat_events import write_event
from wechat_paths import DATA_ROOT, check_drive

JOB_NAME = re.compile(r'[a-f0-9]{32}\.json')
SEARCH_TIMEOUT = 90
TIMEOUT_MESSAGE = '微信搜索已超时，请重新搜索。'


class SearchDeadlineExceeded(Exception):
    """The whole search expired, including any capture fallback."""


def timeout_search(signum, frame):
    raise SearchDeadlineExceeded(TIMEOUT_MESSAGE)


def run_request(path, save_lock, search):
    try:
        with path.open('rb') as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError
        data = json.loads(raw)
        query, expires = data['q'], data['expires']
        kind = data.get('kind', 'video')
        if (kind not in ('video', 'public') or not isinstance(query, str) or not query.strip() or len(query) > 128
                or any(ord(ch) < 32 or ch == '\x7f' for ch in query)
                or type(expires) not in (int, float) or not math.isfinite(expires)):
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        return {'error': '搜索请求格式有误，请重新搜索。'}, ('warning', '微信搜索请求格式无效。')
    seconds = min(SEARCH_TIMEOUT, expires - time.time())
    if seconds <= 0:
        event = ('warning', '微信搜索在排队期间超时。') if expires > 0 else None
        return {'error': TIMEOUT_MESSAGE}, event
    with save_lock.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'error': '微信播放器正被保存任务或部署占用，请稍后重试。'}, (
                'warning', '微信播放器被占用，搜索暂未开始。')
        previous_handler = signal.signal(signal.SIGALRM, timeout_search)
        try:
            signal.setitimer(signal.ITIMER_REAL, seconds)
            if kind == 'public':
                from wechat_public_discovery import search_public_accounts
                results = search_public_accounts(query.strip())
            else:
                results = search(query.strip())
        except (SearchDeadlineExceeded, TimeoutError):
            return {'error': TIMEOUT_MESSAGE}, ('warning', '微信搜索执行超时。')
        except RuntimeError as exc:
            return {'error': str(exc)}, ('error', '微信搜索未能完成。')
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    return {'results': results}, ('info', f'微信搜索完成，返回 {len(results)} 个候选账号。')


def write_reply(path, data):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as file:
        pending = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def process_queue(monitoring, save_lock, search):
    requests, replies = monitoring/'search-requests', monitoring/'search-replies'
    # Keep manual/recovery invocations from acknowledging a request that
    # the background worker is still processing.
    with (monitoring/'search-worker.lock').open('a+b') as worker_lock:
        try:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for reply in replies.glob('*.json'):
            try:
                if JOB_NAME.fullmatch(reply.name) and reply.lstat().st_mtime < time.time() - 600:
                    reply.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        pending = []
        for path in requests.glob('*.json'):
            if JOB_NAME.fullmatch(path.name):
                try:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode):
                        pending.append((info.st_mtime_ns, path))
                except FileNotFoundError:
                    pass
        for _, path in sorted(pending):
            reply = replies/path.name
            if not reply.exists():
                try:
                    response, event = run_request(path, save_lock, search)
                except FileNotFoundError:
                    if not path.exists():
                        continue
                    response = {'error': '微信搜索服务文件缺失，请检查部署。'}
                    event = ('error', '微信搜索服务文件缺失。')
                except Exception as exc:
                    print(f'微信搜索失败：{type(exc).__name__}', file=sys.stderr)
                    response = {'error': '微信搜索服务暂时不可用，请检查微信桌面后重试。'}
                    event = ('error', '微信搜索服务暂时不可用。')
                if path.exists():
                    write_reply(reply, response)
                if event:
                    write_event(monitoring, 'search', *event)
            path.unlink(missing_ok=True)


def search_wechat(query):
    from wechat_account_search import search_accounts
    return search_accounts(query)


def run_pending():
    monitoring = DATA_ROOT/'state/monitoring'
    if not (any((monitoring/'search-requests').glob('*.json'))
            or any((monitoring/'search-replies').glob('*.json'))):
        return
    check_drive()
    process_queue(monitoring, DATA_ROOT/'state/wechat-pilot/save.lock', search_wechat)


def serve():
    check_drive()
    monitoring = DATA_ROOT/'state/monitoring'
    script = Path(__file__).resolve()
    with (monitoring/'search-service.lock').open('a+b') as service:
        try:
            fcntl.flock(service, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        version = script.stat().st_ino
        cleanup = time.monotonic() + 60
        while monitoring.is_dir():
            if script.stat().st_ino != version:
                # install.py atomically replaces changed scripts. Restart against
                # that version; the close-on-exec lock is retaken on startup.
                os.execv(sys.executable, [sys.executable, '-B', str(script), '--serve'])
            pending = any((monitoring/'search-requests').glob('*.json'))
            if pending or time.monotonic() >= cleanup:
                # Each job loads the currently deployed provider and OCR helpers.
                subprocess.run([sys.executable, '-B', str(script)])
                cleanup = time.monotonic() + 60
            time.sleep(.5)


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve()
    else:
        run_pending()
