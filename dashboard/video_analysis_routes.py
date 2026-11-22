"""Dashboard and worker endpoints for stored video analysis jobs."""
import re
import sqlite3
import time

from video_analysis_queue import LeaseError, public_job


VIDEO_PATH = re.compile(r'/api/videos/([0-9a-f]{64})/analysis')
COLLECTION_PATH = '/api/video-analyses'
WORKER_PATH = '/api/video-analysis-worker'
WORKER_KEY = 'video-analysis-worker.key'
JOB_FIELD = {'job_id', 'lease_token'}
WORKER_FIELDS = {
    'claim': set(),
    'tick': set(),
    'heartbeat': JOB_FIELD,
    'update': JOB_FIELD | {'stage', 'completed', 'total'},
    'finish': JOB_FIELD | {'result'},
    'fail': JOB_FIELD | {'error', 'retryable'},
}
JOB_ID = re.compile(r'[0-9a-f]{32}')
WORKER_TIMEOUT = 25
MAX_WORKER_BODY = 8 * 1024 * 1024


def worker_call(queue, data):
    op = data.get('op')
    fields = data.keys() - {'op'}
    if not isinstance(op, str) or op not in WORKER_FIELDS or fields != WORKER_FIELDS[op]:
        raise ValueError('视频分析进程操作无效。')
    if op == 'claim':
        return {'job': queue.claim()}
    if op == 'tick':
        return {'ok': True}
    if any(not isinstance(data[field], str) or not JOB_ID.fullmatch(data[field]) for field in JOB_FIELD):
        raise ValueError('分析任务与租约标识无效。')
    try:
        if op == 'heartbeat':
            queue.heartbeat(data['job_id'], data['lease_token'])
        elif op == 'update':
            queue.update(data['job_id'], data['lease_token'], data['stage'], data['completed'], data['total'])
        elif op == 'finish':
            queue.finish(data['job_id'], data['lease_token'], data['result'])
        elif op == 'fail':
            queue.fail(data['job_id'], data['lease_token'], data['error'], data['retryable'])
    except (KeyError, LeaseError):
        return {'ok': False}
    return {'ok': True}


def analysis_summaries(queue, library):
    summaries = queue.list_summaries()
    if not summaries:
        return []
    recordings = {(row['work_key'], row['sha256']): row for row in library.recordings(include_digest=True)}
    jobs = []
    for summary in summaries:
        row = recordings.get((summary['work_key'], summary.pop('sha256')))
        if row is not None and library.available_for_analysis(row):
            jobs.append(dict(summary, **library.recording_display(row)))
    return jobs


def analysis_reply(handler, path, work_key):
    server = handler.server
    queue = server.video_analysis
    if path == WORKER_PATH:
        if handler.command != 'POST':
            return 405, {'error': '视频分析进程仅可提交 POST 请求。'}
        if not server.worker_authorized(handler.headers.get('X-Video-Analysis-Worker', ''), WORKER_KEY):
            return 403, {'error': '仅视频分析进程可访问。'}
        data = handler.read_json(max_length=MAX_WORKER_BODY)
        if data is None:
            return
        reply = worker_call(queue, data)
        server.last_video_analysis_tick = time.monotonic()
        return 200, reply

    method = handler.command
    worker_online = time.monotonic() - server.last_video_analysis_tick < WORKER_TIMEOUT
    if path == COLLECTION_PATH:
        if method not in ('GET', 'HEAD'):
            return 405, {'error': '视频分析列表仅支持查询。'}
        return 200, {'jobs': analysis_summaries(queue, server.library), 'worker_online': worker_online}
    if method not in ('GET', 'HEAD', 'POST'):
        return 405, {'error': '视频分析仅支持查询与提交。'}
    if method == 'POST':
        data = handler.read_json()
        if data is None:
            return
        if data:
            raise ValueError('请通过已入库视频提交分析。')
    recording = server.library.analysis_recording(work_key)
    if recording is None:
        return 404, {'error': '未找到可分析的录像。'}
    if method == 'POST':
        code, job = 202, queue.enqueue(recording)
    else:
        code, job = 200, queue.get(work_key, recording['sha256'])
    return code, {'job': public_job(job), 'worker_online': worker_online}


def handle(handler, path, work_key):
    try:
        reply = analysis_reply(handler, path, work_key)
    except ValueError as err:
        reply = 400, {'error': str(err)}
    except (OSError, sqlite3.Error) as err:
        handler.log_error('Video analysis unavailable: %s', err)
        reply = 503, {'error': '视频分析暂时不可用，请稍后重试。'}
    if reply is not None:
        handler.json_reply(*reply)
