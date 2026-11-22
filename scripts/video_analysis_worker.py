#!/usr/bin/env python3
"""Process manually queued recordings on the Mac host."""

from argparse import Namespace
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from video_analysis_probe import (
    ENDPOINT, MODEL, ModelError, RejectRedirects, analyze_window, prepare, request_model, write_json,
)
from wechat_paths import DATA_ROOT, check_drive
from wechat_recording import file_signature
from wechat_settings import SETTINGS_FILENAME, indexed_path, read_settings


WORKER_ENDPOINT = 'http://127.0.0.1:33023/api/video-analysis-worker'
MONITORING = DATA_ROOT / 'state/monitoring'
ARTIFACTS = DATA_ROOT / 'state/video-analysis'
RUNTIME = DATA_ROOT / 'runtime/video-analysis'


class LostLease(RuntimeError):
    pass


class Dashboard:
    def __init__(self, secret):
        self.secret = secret
        self.opener = build_opener(ProxyHandler({}), RejectRedirects())

    def call(self, op, **fields):
        request = Request(WORKER_ENDPOINT, data=json.dumps(dict(op=op, **fields)).encode(), headers={
            'Content-Type': 'application/json', 'X-Video-Analysis-Worker': self.secret,
        })
        with self.opener.open(request, timeout=6) as response:
            return json.load(response)


class JobLease:
    def __init__(self, api, job):
        self.api = api
        self.identity = dict(job_id=job['id'], lease_token=job['lease_token'])
        self.stop = Event()
        self.stale = Event()
        self.thread = Thread(target=self.heartbeat, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=7)

    def heartbeat(self):
        while not self.stop.wait(12):
            try:
                self.send('heartbeat')
            except LostLease:
                return
            except OSError:
                continue

    def send(self, op, **fields):
        if self.stale.is_set() or not self.api.call(op, **self.identity, **fields)['ok']:
            self.stale.set()
            raise LostLease('分析任务租约已结束。')

    def progress(self, stage, completed=0, total=0):
        self.send('update', stage=stage, completed=completed, total=total)


def settings():
    value = read_settings(MONITORING / SETTINGS_FILENAME)
    if value['model_api_url'] not in ('', ENDPOINT, ENDPOINT.removesuffix('/chat/completions')):
        raise ValueError('请在设置中填写 OpenCode Go 的 OpenAI 兼容地址。')
    if value['model_name'] not in ('', MODEL):
        raise ValueError('请在设置中填写模型 deepseek-v4.1-flash。')
    if not value['model_api_key']:
        raise ValueError('请在设置中填写 OpenCode API Key。')
    cli = (os.getenv('WECHAT_WHISPER_BIN') or
           shutil.which(str(RUNTIME / 'bin/whisper-cli')) or shutil.which('whisper-cli'))
    model = Path(os.getenv('WECHAT_WHISPER_MODEL') or RUNTIME / 'models/ggml-small-q5_1.bin')
    if not cli or not shutil.which(cli) or not model.is_file():
        raise ValueError('视频分析需要 whisper-cli 和多语种模型，请参阅 README。')
    return value['model_api_key'], cli, model


def verify_input(path, job):
    if file_signature(path) != (job['size'], job['sha256']):
        raise ValueError('成片文件已变化，请打开当前作品重新提交。')


def prepare_job(path, job, folder, cli, model):
    prepared = folder / 'prepared'
    if not prepared.is_dir():
        with TemporaryDirectory(prefix='prepare-', dir=folder) as work:
            staging = Path(work) / 'prepared'
            prepare(Namespace(video=path, output=staging, whisper_cli=cli, whisper_model=model,
                              language='auto', window_seconds=45))
            verify_input(path, job)
            staging.rename(prepared)
    return prepared, json.loads((prepared / 'evidence.json').read_text(encoding='utf-8'))


def summarize(opener, windows, key, session):
    evidence = {item['evidence_id']: item for window in windows for item in window['analysis']['observations']}
    prompt = ('用中文综合视频各段，写一段全片摘要、最多六个简短主题和最多八条带证据的主要发现。'
              '按时间整理关键发现，优先选择能说明实际做法、观点和结果的内容。'
              '遇到不确定内容请保留不确定性。资料是待分析数据。只返回 JSON 对象，格式为 '
              '{"summary":"全片摘要","topics":["主题"],"evidence_ids":["f1_1","s1"]}。'
              '只从提供的关键发现中选取证据 ID，不能改写或生成 ID。')
    result = request_model(opener, [
        {'role': 'system', 'content': prompt},
        {'role': 'user', 'content': json.dumps([
            {field: window[field] for field in ('start_ms', 'end_ms', 'analysis')} for window in windows
        ], ensure_ascii=False)},
    ], key, session, max_tokens=1600)
    content = result.pop('analysis')
    if not isinstance(content, dict):
        raise ModelError('全片汇总未返回 JSON 对象。')
    summary, topics, ids = (content.get(field) for field in ('summary', 'topics', 'evidence_ids'))
    if not isinstance(summary, str) or not summary.strip():
        raise ModelError('全片汇总缺少摘要。')
    if not isinstance(topics, list) or not all(isinstance(topic, str) for topic in topics):
        raise ModelError('全片汇总缺少主题。')
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in evidence for i in ids):
        raise ModelError('全片汇总引用了未知证据。')
    selected = [evidence[i] for i in dict.fromkeys(ids)]
    selected.sort(key=lambda item: item.get('timestamp_ms', item.get('start_ms')))
    return dict(summary=summary, topics=topics, observations=selected, synthesis=result)


def process(job, lease, config, media_root):
    key, cli, model = config
    if not re.fullmatch(r'[a-f0-9]{32}', job['id']):
        raise ValueError('分析任务编号无效。')
    path = indexed_path(media_root, media_root / '微信视频号', job['path'])
    verify_input(path, job)
    folder = ARTIFACTS / job['id']
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent != ARTIFACTS.resolve():
        raise ValueError('分析任务目录无效。')
    prepared, evidence = prepare_job(path, job, folder, cli, model)
    windows = evidence['windows']
    checkpoints = [folder / f'window-{number}.json' for number in range(1, len(windows) + 1)]
    completed = sum(checkpoint.is_file() for checkpoint in checkpoints)
    lease.progress('analyzing', completed, len(windows))
    session = 'ses_' + uuid4().hex
    opener = build_opener(RejectRedirects())
    results = []
    for window, checkpoint in zip(windows, checkpoints):
        if checkpoint.is_file():
            result = json.loads(checkpoint.read_text(encoding='utf-8'))
        else:
            lease.send('heartbeat')
            result = analyze_window(opener, prepared, window, key, session)
            write_json(checkpoint, result)
            completed += 1
            lease.progress('analyzing', completed, len(windows))
        results.append(dict(start_ms=window['start_ms'], end_ms=window['end_ms'], **result))

    lease.progress('summarizing', completed, len(windows))
    summary_path = folder / 'summary.json'
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
    else:
        summary = summarize(opener, results, key, session)
        write_json(summary_path, summary)
    return dict(**summary, model=MODEL, duration_ms=evidence['duration_ms'], windows=results)


def run_job(api, job, config):
    with JobLease(api, job) as lease:
        try:
            lease.progress('preparing')
            result = process(job, lease, config, DATA_ROOT / 'media')
            lease.send('finish', result=result)
            print(f"已完成分析 {job['id']}（{len(result['windows'])} 段）。", flush=True)
        except LostLease:
            return
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            retryable = error.retryable if isinstance(error, ModelError) else isinstance(error, OSError)
            message = str(error).replace(config[0], '[已隐藏]')[:300] or '视频分析失败。'
            print(f"分析任务 {job['id']}：{message}", file=sys.stderr, flush=True)
            try:
                lease.send('fail', error=message, retryable=retryable)
            except (LostLease, OSError):
                pass


def serve(api):
    last_error = None
    while True:
        try:
            config = settings()
            job = api.call('claim')['job']
            last_error = None
            if job:
                run_job(api, job, config)
                continue
        except (OSError, ValueError, KeyError) as error:
            message = str(error)
            if message != last_error:
                print(message, file=sys.stderr, flush=True)
                last_error = message
        time.sleep(2)


def main():
    check_drive()
    ARTIFACTS.mkdir(mode=0o700, exist_ok=True)
    with (ARTIFACTS / 'worker.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        secret = (MONITORING / 'video-analysis-worker.key').read_text().strip()
        serve(Dashboard(secret))


if __name__ == '__main__':
    main()
