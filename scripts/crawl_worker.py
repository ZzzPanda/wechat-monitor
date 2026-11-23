"""Run the dashboard's history and incremental jobs on the signed-in desktop."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from wechat_events import write_event
from wechat_paths import DATA_ROOT, check_drive

MONITORING = DATA_ROOT / 'state/monitoring'
PLAYER_LOCK = DATA_ROOT / 'state/wechat-pilot/save.lock'
ENDPOINT = f"http://127.0.0.1:{int(os.environ.get('WECHAT_PORT_BASE', 33021)) + 2}/api/crawl-worker"


class ServiceError(Exception):
    pass


class RequestRejected(ServiceError):
    """The dashboard rejected an item or its job changed during collection."""


class Dashboard:
    def __init__(self):
        self.secret = (MONITORING / 'crawl-worker.key').read_text().strip()
        self.opener = build_opener(ProxyHandler({}))

    def __call__(self, op, **fields):
        data = json.dumps(dict(op=op, **fields), ensure_ascii=False).encode()
        request = Request(ENDPOINT, data=data, headers={'Content-Type': 'application/json',
                                                       'X-Crawl-Worker': self.secret})
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get('error')
            except (ValueError, OSError):
                message = ''
            error = RequestRejected if exc.code in (400, 404, 409, 413) else ServiceError
            raise error(message or f'管理页响应 {exc.code}') from exc
        except (URLError, OSError, ValueError) as exc:
            raise ServiceError('采集管理页暂时无法连接。') from exc


@contextmanager
def try_lock(path):
    with path.open('a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def run_job(api, job, saver, db, discover):
    job_id, name, profile = job['id'], job['author'], job['profile_url']
    mode = job['mode']
    since = None
    if mode == 'incremental':
        prior = api('seen_before', profile_url=profile)
        if not prior['known']:
            mode = 'baseline'
        elif prior.get('since') is not None:
            beijing = timezone(timedelta(hours=8))
            since = datetime.fromtimestamp(prior['since'], beijing).date().isoformat()
    entry = dict(name=name, url=profile)
    details = {}
    failed_keys = set()
    unverified_cards = None
    processed = 0
    checkpoint = job.get('checkpoint')

    def stopped():
        return not api('active', id=job_id, author=name, profile_url=profile)['active']

    def yield_at_boundary(position):
        state = api('active', id=job_id, author=name, profile_url=profile, boundary=True)
        if not state.get('yield_requested'):
            return False
        return api('yield_history', id=job_id, checkpoint=position)['yielded']

    def metadata(url):
        if not saver['preview'].SHARE_URL.fullmatch(url):
            raise ValueError('微信没有返回有效的作品分享链接。')
        if url not in details:
            item = saver['work'](db, url, fetch=True)
            if item['author'] != name:
                raise ValueError('分享页作者与监控账号不符。')
            details[url] = item
        return details[url]

    item_errors = (ValueError, RuntimeError, OSError, sqlite3.Error,
                   subprocess.SubprocessError, saver['SaveError'])

    def known(url):
        # A new share URL can point at the same work. Match its verified fingerprint.
        try:
            work = metadata(url)
        except item_errors:
            return False
        # The preview gives a calendar date. Keep same-day posts eligible and
        # resolve them by fingerprint, even when several were published together.
        if since and work.get('published') and work['published'] < since:
            return True
        return api('known', profile_url=profile, key=work['fingerprint'])['known']

    def handle(url):
        nonlocal processed
        if stopped():
            return
        item = None
        key = None
        try:
            item = metadata(url)
            key = item['fingerprint']
            record = api('record', id=job_id, url=url, key=key)['item']
            link = record['url']
            # The same card may return another share URL after scrolling. Give
            # each failed work one attempt per pass, including after recovery.
            if (record['status'] in ('saved', 'skipped', 'baseline') or key in failed_keys
                    or (checkpoint is not None and record['status'] == 'failed')):
                return
            if mode == 'baseline':
                status = 'baseline'
            else:
                saved = saver['save'](db, url, item, return_to_profile=False)
                if saved['status'] == 'saved':
                    status = 'saved'
                    saver['update_poster'](saved['work_key'])
                elif saved['status'] == 'already_saved':
                    status = 'skipped'
                else:
                    raise ValueError('作品尚未保存完成。')
            api('item_done', id=job_id, url=link, status=status)
            processed += 1
        except item_errors as exc:
            message = str(exc)[:350] or '作品处理失败。'
            title = (item or {}).get('caption', '').splitlines()
            if title:
                message = f'《{title[0][:65]}》{message}'
            if key is None:
                key = 'unresolved:' + hashlib.sha256(url.encode()).hexdigest()
                link = api('record', id=job_id, url=url, key=key)['item']['url']
            api('item_done', id=job_id, url=link, status='failed', error=message)
            if (getattr(exc, 'status', '') in ('disk_full', 'disk_unavailable', 'runtime_unavailable', 'preview_unavailable')
                    or getattr(exc, 'errno', None) in (errno.ENOSPC, errno.EROFS, errno.ENODEV, errno.EIO)
                    or isinstance(exc, sqlite3.OperationalError)):
                raise RuntimeError(f'采集已停止。{message}') from exc
            failed_keys.add(key)
        finally:
            details.pop(url, None)

    try:
        resume = dict(checkpoint=checkpoint, on_boundary=yield_at_boundary) if mode == 'history' else {}
        outcome = discover(entry, mode=mode, known=known, on_work=handle, should_stop=stopped, **resume)
        unverified_cards = outcome.get('unverified_cards')
        if outcome.get('yielded'):
            # The same job is already queued with its checkpoint. A finish or
            # cancellation here would discard the remainder of the history scan.
            return
        if stopped():
            status, message = 'cancelled', '任务已停止或监控名单已变更。'
        elif not outcome['complete'] or failed_keys:
            status, message = 'partial', outcome.get('reason', '')
        else:
            status = 'done'
            message = ('已记录首批作品，后续定时任务保存新增作品。' if mode == 'baseline'
                       else outcome.get('reason', ''))
    except ServiceError:
        # A dashboard restart must leave the job recoverable on the next connection.
        raise
    except Exception as exc:
        status = 'partial' if processed or failed_keys else 'failed'
        message = str(exc)[:400] or '采集未完成。'
    api('finish', id=job_id, status=status, error=message[:500], unverified_cards=unverified_cards)


def run_public_job(api, job, discover, read_article):
    job_id = job['id']
    since = job.get('since')
    cutoff = (datetime.fromtimestamp(since, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
              if since is not None else None)
    saved_or_checked = failures = invalid_links = 0

    def stopped():
        return not api('public_active', id=job_id)['active']

    def collect(url):
        nonlocal saved_or_checked, failures, invalid_links
        try:
            known = api('public_skip_known', id=job_id, url=url)
        except RequestRejected:
            failures += 1
            invalid_links += 1
            return False
        if known['published'] or known.get('before_cutoff'):
            saved_or_checked += 1
            return bool(known.get('before_cutoff') or (cutoff and known['published'] < cutoff))
        try:
            article = read_article(url)
            if article['biz'] != job['account']['biz']:
                raise ValueError('文章署名所属的公众号与所选账号不一致。')
        except (RuntimeError, ValueError, OSError) as exc:
            api('public_fail', id=job_id, url=url, error=str(exc)[:400])
            failures += 1
            return False
        if stopped():
            return False
        older = bool(cutoff and article['published'] < cutoff)
        try:
            api('public_skip_before' if older else 'public_save', id=job_id, article=article)
        except RequestRejected as exc:
            if not stopped():
                api('public_fail', id=job_id, url=url, error=str(exc)[:400])
                failures += 1
            return False
        saved_or_checked += 1
        return older

    def yield_at(checkpoint):
        return api('public_yield', id=job_id, checkpoint=checkpoint)['yielded']

    try:
        resume = {}
        if job['mode'] == 'history' and not job.get('max_pages'):
            resume = dict(checkpoint=job.get('checkpoint'), on_boundary=yield_at)
        outcome = discover(job['account'], mode=job['mode'], on_article=collect,
                           should_stop=stopped, max_pages=job.get('max_pages'), **resume)
        if outcome.get('yielded') or stopped():
            return
        # The queue counts outstanding item failures when finishing. A later
        # verified alias may have resolved a read error from this traversal.
        status = 'done' if outcome['complete'] and not invalid_links else 'partial'
        message = outcome.get('reason', '')
        if invalid_links:
            message += f'。{invalid_links} 个文章链接未通过校验。'
    except RequestRejected as exc:
        if stopped():
            return
        status, message = 'partial' if saved_or_checked or failures else 'failed', str(exc)
    except ServiceError:
        # The supervisor requeues a running job after a transport interruption.
        raise
    except Exception as exc:
        status = 'partial' if saved_or_checked or failures else 'failed'
        message = str(exc) or '公众号采集未完成。'
    api('public_finish', id=job_id, status=status, error=message[:500])


def run_one(api):
    # The single player stays on this author's grid between recordings.
    with try_lock(MONITORING / 'crawl-run.lock') as worker, try_lock(PLAYER_LOCK) as player:
        if not (worker and player):
            return
        job = api('claim')['job']
        if not job:
            return
        try:
            if job.get('source') == 'public':
                from wechat_public_article import read_article
                from wechat_public_discovery import crawl_public_account
                run_public_job(api, job, crawl_public_account, read_article)
                return
            from wechat_discovery import crawl_profile
            saver = runpy.run_path(str(Path(__file__).with_name('wechat-save')))
            saver['verify_disk']()
            with sqlite3.connect(saver['preview'].DATABASE, timeout=15) as db:
                db.row_factory = sqlite3.Row
                saver['preview'].prepare(db)
                run_job(api, job, saver, db, crawl_profile)
        except ServiceError:
            raise
        except Exception as exc:
            op = 'public_finish' if job.get('source') == 'public' else 'finish'
            api(op, id=job['id'], status='failed', error=(str(exc) or '采集进程未能启动。')[:500])


def serve():
    script = Path(__file__).resolve()
    with try_lock(MONITORING / 'crawl-service.lock') as owner:
        if not owner:
            return
        version = script.stat().st_ino
        child = None
        recover = True
        unavailable = False
        api = Dashboard()
        while MONITORING.is_dir():
            try:
                if child and child.poll() is not None:
                    recover = child.returncode != 0
                    if recover:
                        write_event(MONITORING, 'crawl', 'error', '采集工作进程异常退出，正在恢复任务队列。')
                    child = None
                if not child and script.stat().st_ino != version:
                    os.execv(sys.executable, [sys.executable, '-B', str(script), '--serve'])
                if recover:
                    with try_lock(MONITORING / 'crawl-run.lock') as idle:
                        if idle:
                            api('recover')
                            recover = False
                queued = api('tick')['queued']
                if queued and child is None and not recover:
                    child = subprocess.Popen([sys.executable, '-B', str(script), '--one'])
                if unavailable:
                    write_event(MONITORING, 'crawl', 'info', '采集调度已恢复。')
                    unavailable = False
            except (ServiceError, OSError) as exc:
                if not unavailable:
                    message = ('采集进程暂时无法联系管理页，稍后重试。' if isinstance(exc, ServiceError)
                               else '采集调度遇到本地文件或进程错误，稍后重试。')
                    write_event(MONITORING, 'crawl', 'error', message)
                    unavailable = True
                print(str(exc), file=sys.stderr, flush=True)
            time.sleep(4)


if __name__ == '__main__':
    check_drive()
    if sys.argv[1:] == ['--serve']:
        serve()
    elif sys.argv[1:] == ['--one']:
        run_one(Dashboard())
    else:
        sys.exit('请选择 --serve 或 --one。')
