"""Cache the pinned arm64 base images as OCI layouts for a local BuildKit context."""
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REGISTRIES = {
    'lscr.io/linuxserver/weixin': ('ghcr.io', 'linuxserver/weixin', 'https://ghcr.io/token', 'ghcr.io'),
    'docker.io/library/python': ('registry-1.docker.io', 'library/python', 'https://auth.docker.io/token', 'registry.docker.io'),
}
ACCEPT = ', '.join(('application/vnd.oci.image.manifest.v1+json', 'application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json', 'application/vnd.docker.distribution.manifest.list.v2+json'))
CHUNK = 8 * 1024**2
READ_SIZE = 512 * 1024
WORKERS = 10


def digest_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('镜像摘要需要固定为 SHA-256')
    return value[7:]


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as inp:
        for block in iter(lambda: inp.read(READ_SIZE), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path, data):
    with tempfile.TemporaryDirectory(dir=path.parent) as work:
        tmp = Path(work) / 'file'
        tmp.write_bytes(data)
        os.replace(tmp, path)


def open_https(request, *, timeout):
    response = urlopen(request, timeout=timeout)
    if not response.geturl().startswith('https://'):
        response.close()
        raise ValueError('镜像下载被重定向到了非 HTTPS 地址')
    return response


class Registry:
    def __init__(self, name):
        domain, repo, auth, service = REGISTRIES[name]
        self.url = 'https://' + domain + '/v2/' + repo
        self.auth = auth + '?' + urlencode({'service': service, 'scope': f'repository:{repo}:pull'})
        self.lock = threading.Lock()
        self.token = None
        self.expires = 0

    def get(self, endpoint, headers):
        for attempt in range(2):
            with self.lock:
                if self.token is None or time.monotonic() >= self.expires:
                    with open_https(self.auth, timeout=30) as response:
                        data = json.loads(response.read(65536))
                    self.token = data['token']
                    self.expires = time.monotonic() + max(1, int(data.get('expires_in', 180)) - 15)
                token = self.token
            req = Request(self.url + '/' + endpoint, headers={'Accept-Encoding': 'identity', **headers})
            # The anonymous registry credential is scoped to this origin. CDN redirects get no credentials.
            req.add_unredirected_header('Authorization', 'Bearer ' + token)
            try:
                return open_https(req, timeout=60)
            except HTTPError as err:
                if err.code != 401 or attempt:
                    raise
                err.close()
                with self.lock:
                    if self.token == token:
                        self.token = None


def retry(function):
    for attempt in range(4):
        try:
            return function()
        except (HTTPError, URLError, HTTPException, OSError) as err:
            if isinstance(err, HTTPError):
                err.close()
            if attempt == 3 or isinstance(err, HTTPError) and err.code in (400, 401, 404):
                raise
            time.sleep(2 ** attempt)


def manifest(registry, blobs, key):
    target = blobs / digest_name(key)
    if target.is_file() and file_hash(target) == target.name:
        return json.loads(target.read_bytes()), target.stat().st_size

    def fetch():
        with registry.get('manifests/' + key, {'Accept': ACCEPT}) as response:
            data = response.read(8 * 1024**2 + 1)
        if hashlib.sha256(data).hexdigest() != target.name:
            raise ValueError('上游镜像清单与固定摘要不符')
        atomic_write(target, data)
        return json.loads(data), len(data)
    return retry(fetch)


class NoRange(Exception):
    pass


def fetch_part(registry, key, total, offset, length, dest, ranged):
    def transfer():
        end = offset + length - 1
        headers = {'Range': f'bytes={offset}-{end}'} if ranged else {}
        with registry.get('blobs/' + key, headers) as response:
            if ranged:
                if response.status == 200:
                    raise NoRange()
                if response.status != 206 or response.headers.get('Content-Range') != f'bytes {offset}-{end}/{total}':
                    raise OSError('容器镜像下载返回了不匹配的分段位置')
            elif response.status != 200:
                raise OSError('镜像服务器没有返回完整文件')
            with tempfile.TemporaryDirectory(dir=dest.parent, prefix='.partial-') as work:
                tmp = Path(work) / 'part'
                with tmp.open('wb') as output:
                    remaining = length
                    while remaining:
                        block = response.read(min(READ_SIZE, remaining))
                        if not block:
                            raise OSError('容器镜像下载中断')
                        output.write(block)
                        remaining -= len(block)
                    if response.read(1):
                        raise OSError('容器镜像分段超出预期长度')
                os.replace(tmp, dest)
    retry(transfer)


def download_blob(registry, blobs, scratch, desc, workers):
    key, size = desc['digest'], desc['size']
    target = blobs / digest_name(key)
    if type(size) is not int or size < 0:
        raise ValueError('镜像清单中的文件长度无效')
    if target.is_file() and target.stat().st_size == size and file_hash(target) == target.name:
        return
    parts = scratch / target.name
    parts.mkdir(exist_ok=True)
    for abandoned in parts.glob('.partial-*'):
        if abandoned.is_dir() and not abandoned.is_symlink():
            shutil.rmtree(abandoned)
    offsets = list(range(0, size, CHUNK))
    if size > CHUNK:
        print(f'下载镜像层 {target.name[:12]}（{size // 1024**2} MiB），继续使用已校验长度的分段。', flush=True)
    def download(offset):
        length = min(CHUNK, size-offset)
        part = parts / str(offset)
        if not part.is_file() or part.stat().st_size != length:
            fetch_part(registry, key, size, offset, length, part, size > CHUNK)

    # Probe once so a server ignoring Range receives only one full download.
    whole = parts / 'whole'
    try:
        if offsets:
            download(0)
        pending = {workers.submit(download, offset): min(CHUNK, size-offset) for offset in offsets[1:]}
        received = min(size, CHUNK)
        reported = time.monotonic()
        try:
            for future in as_completed(pending):
                future.result()
                received += pending[future]
                if time.monotonic() - reported >= 20:
                    print(f'镜像层 {target.name[:12]} 已完成 {received // 1024**2}/{size // 1024**2} MiB', flush=True)
                    reported = time.monotonic()
        finally:
            for future in pending:
                future.cancel()
            wait(pending)
        with whole.open('wb') as out:
            for offset in offsets:
                with (parts / str(offset)).open('rb') as inp:
                    shutil.copyfileobj(inp, out, length=READ_SIZE)
    except NoRange:
        fetch_part(registry, key, size, 0, size, whole, False)
    if whole.stat().st_size != size or file_hash(whole) != target.name:
        shutil.rmtree(parts)
        raise ValueError(f'镜像层未通过 SHA-256 校验：{key}；请重试以重新下载此层。')
    os.replace(whole, target)
    shutil.rmtree(parts)


def prepare(reference, target):
    target = Path(target)
    image, separator, key = reference.partition('@')
    name = image.split(':', 1)[0]
    if not separator or name not in REGISTRIES:
        raise ValueError('仅支持已锁定的微信和 Python 基础镜像')
    digest_name(key)
    target.mkdir(parents=True, exist_ok=True)
    blobs = target/'blobs/sha256'
    blobs.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + '.downloads')
    scratch.mkdir(exist_ok=True)
    registry = Registry(name)
    with target.with_name(target.name + '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc, length = manifest(registry, blobs, key)
        if 'manifests' in doc:
            entries = [e for e in doc['manifests'] if e.get('platform', {}).get('os') == 'linux' and
                       e['platform'].get('architecture') == 'arm64' and e['platform'].get('variant', 'v8') == 'v8']
            if len(entries) != 1:
                raise ValueError('未找到唯一的 Linux ARM64 镜像')
            key = entries[0]['digest']
            doc, length = manifest(registry, blobs, key)
            if length != entries[0]['size']:
                raise ValueError('镜像清单长度不符')
        config = doc['config']
        with ThreadPoolExecutor(max_workers=WORKERS) as workers:
            for desc in [config, *doc['layers']]:
                download_blob(registry, blobs, scratch, desc, workers)
        architecture = json.loads((blobs/digest_name(config['digest'])).read_bytes())
        if (architecture.get('os'), architecture.get('architecture')) != ('linux', 'arm64'):
            raise ValueError('基础镜像不是 Linux ARM64')
        index = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [
            {'mediaType': doc['mediaType'], 'digest': key, 'size': length,
             'platform': {'os': 'linux', 'architecture': 'arm64'},
             'annotations': {'org.opencontainers.image.ref.name': 'base'}}]}
        atomic_write(target/'oci-layout', b'{"imageLayoutVersion":"1.0.0"}\n')
        atomic_write(target/'index.json', json.dumps(index).encode() + b'\n')
    return target
