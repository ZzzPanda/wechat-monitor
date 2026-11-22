"""Local API for public account searches, schedules and archived article text."""
import re
from urllib.parse import parse_qs, urlsplit

from account_search import valid_text
from public_articles import AccountConflict


def public_candidate(row):
    if not isinstance(row, dict) or not valid_text(row.get('name'), 64):
        return None
    biz = row.get('id')
    if not isinstance(biz, str) or not re.fullmatch(r'[A-Za-z0-9+/_=-]{3,200}', biz):
        return None
    return {'name': row['name'].strip(), 'id': biz, 'biz': biz,
            'description': row.get('description', '') if valid_text(row.get('description'), 512) else ''}


def article_list(store, query):
    params = parse_qs(query, keep_blank_values=True, max_num_fields=5)
    if params.keys() - {'account_id', 'offset', 'limit', 'q', 'sort'} or any(len(v) != 1 for v in params.values()):
        raise ValueError('文章列表查询格式无效。')
    account = params.get('account_id', [None])[0] or None
    if account and not re.fullmatch(r'[a-f0-9]{64}', account):
        raise ValueError('请选择有效的公众号。')
    try:
        limit, offset = int(params.get('limit', ['50'])[0]), int(params.get('offset', ['0'])[0])
    except ValueError:
        raise ValueError('文章分页参数无效。') from None
    if not 1 <= limit <= 100 or not 0 <= offset < 2**63:
        raise ValueError('文章分页参数超出范围。')
    return store.list_articles(account, limit=limit, offset=offset,
                               q=params.get('q', [''])[0], sort=params.get('sort', ['newest'])[0])


def handle_public(handler, path):
    store = handler.server.public_articles
    method = handler.command
    query = urlsplit(handler.path).query
    if method in ('GET', 'HEAD'):
        if path == '/api/public':
            from time import monotonic
            return 200, dict(store.snapshot(), worker_online=monotonic() - handler.server.last_tick < 25)
        if path == '/api/public/articles':
            return 200, article_list(store, query)
        detail = re.fullmatch(r'/api/public/articles/([A-Za-z0-9_-]{1,128})', path)
        if detail:
            article = store.article(detail[1])
            if article is None:
                raise KeyError(detail[1])
            return 200, article
    if method in ('POST', 'PATCH', 'DELETE'):
        data = handler.read_json()
        if data is None:
            return
        if method == 'POST' and path == '/api/public/accounts' and set(data) == {'token'}:
            choice = handler.server.public_selections.resolve(data['token'])
            account = store.add_account(choice)
            added = account.pop('added')
            return 200, dict(account=account, added=added)
        selected = re.fullmatch(r'/api/public/accounts/([0-9a-f]{64})', path)
        if selected and 'version' in data:
            if method == 'DELETE' and set(data) == {'version'}:
                store.remove_account(selected[1], data['version'])
                return 200, {'removed': True}
            fields = data.keys() - {'version'}
            if method == 'PATCH' and fields and not fields - {'note', 'token'}:
                if 'note' in fields and not isinstance(data['note'], str):
                    raise ValueError('请输入文字备注。')
                choice = handler.server.public_selections.resolve(data['token']) if 'token' in fields else None
                updated = store.update_account(selected[1], data['version'], note=data.get('note'), candidate=choice)
                return 200, dict(account=updated)
        if method == 'POST' and path == '/api/public/history' and set(data) in (
                {'account_id'}, {'account_id', 'max_pages'}):
            if not isinstance(data['account_id'], str):
                raise ValueError('请选择要拉取历史的公众号。')
            return 202, dict(job=store.enqueue(data['account_id'], max_pages=data.get('max_pages')))
        if method == 'PATCH' and path == '/api/public/settings' and set(data) in (
                {'enabled', 'interval_minutes'}, {'enabled', 'interval_minutes', 'start_at'}):
            return 200, dict(settings=store.configure(data['enabled'], data['interval_minutes'], data.get('start_at')))
        cancel = re.fullmatch(r'/api/public/jobs/([0-9a-f]{32})/cancel', path)
        if method == 'POST' and cancel and not data:
            return 200, dict(job=store.cancel(cancel[1]))
        raise ValueError('公众号操作参数无效。')
    return 404, {'error': '公众号接口不存在。'}


def worker_call(store, op, data, video_queue):
    job_id = data['id']
    if op == 'active':
        return dict(active=store.is_running(job_id))
    if op == 'skip_known':
        return dict(published=store.skip_known(job_id, data['url']))
    if op == 'save':
        return dict(status=store.save(job_id, data['article']))
    if op == 'skip_before':
        store.skip_before(job_id, data['article'])
    elif op == 'fail':
        store.fail(job_id, data['url'], data['error'])
    elif op == 'yield':
        if any((q.next_queued() or {}).get('mode') == 'incremental' for q in (store, video_queue)):
            return dict(yielded=store.yield_history(job_id, data['checkpoint']))
        return dict(yielded=False)
    elif op == 'finish':
        store.finish(job_id, data['status'], data.get('error', ''))
    else:
        raise ValueError('公众号采集进程操作无效。')
    return {'ok': True}
