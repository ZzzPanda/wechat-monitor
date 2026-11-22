"""在 Apple Silicon Mac 上安装本地微信采集环境。"""
import argparse
from contextlib import contextmanager
import fcntl
import filecmp
import hashlib
import json
import os
from pathlib import Path
import shlex
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE/'scripts'))
from wechat_events import write_event
from wechat_paths import check_drive, child_on_drive, lima_home, place_vm_disk, prepare_vm_disk, resolve_root, save_selection

IMAGES = json.loads((SOURCE/'deploy/images.lock.json').read_text())
DEFAULT_PORT = 33021


def configure(root, port):
    global APP, RUNTIME, SOCKET, DOCKER, DESKTOP, DASHBOARD
    APP = root
    RUNTIME = root / 'runtime'
    SOCKET = lima_home(root) / 'wx/sock/docker.sock'
    DOCKER = ['docker', '--host', f'unix://{SOCKET}']
    DESKTOP = f'http://127.0.0.1:{port}/'
    DASHBOARD = f'http://127.0.0.1:{port + 2}/'
    os.environ.update(WECHAT_DATA_ROOT=str(root), WECHAT_PORT_BASE=str(port))


GUEST_CHECK = """set -eu
for cmd in ffmpeg ffprobe xdotool xprop xwininfo xsel pactl; do command -v "$cmd" >/dev/null; done
python3 -c 'from PIL import Image; from ctypes import CDLL; CDLL("libX11.so.6")'
xdpyinfo -display :1 >/dev/null
pactl list short sources | grep -q 'output[.]monitor'
"""


def run(*args, **options):
    return subprocess.run([str(a) for a in args], check=True, **options)


def output(*args, **options):
    return run(*args, stdout=subprocess.PIPE, text=True, **options).stdout.strip()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 << 20), b''):
            result.update(block)
    return result.hexdigest()


def check_install_paths():
    for name in ('runtime', 'cache', 'media', 'state', 'tmp', 'scripts', 'tests', 'config', 'images', 'lima', 'vm',
                 'state/wechat-pilot', 'state/monitoring', 'state/posters', 'state/dashboard-app',
                 'state/video-analysis', 'runtime/video-analysis'):
        child_on_drive(APP, APP / name)
    # Docker, Lima and the persistent author list use Unix links on this volume.
    with tempfile.TemporaryDirectory(dir=APP) as stage:
        sample = Path(stage) / 'file'
        sample.touch()
        try:
            (sample.parent / 'symlink').symlink_to(sample.name)
            os.link(sample, sample.parent / 'hardlink')
        except OSError as exc:
            raise RuntimeError(f'数据目录所在卷需要支持符号链接和硬链接，请选择 APFS 等本地卷：{APP}') from exc


def port_base(root, requested):
    saved = child_on_drive(root, root / 'config/port-base')
    previous = int(saved.read_text().strip()) if saved.exists() else None
    vm_exists = (lima_home(root) / 'wx/lima.yaml').is_file()
    if previous is None and vm_exists:
        # Installations created before configurable ports use the original forwards.
        previous = DEFAULT_PORT
    port = previous if previous is not None else DEFAULT_PORT
    if requested is not None:
        port = requested
    if not 1024 <= port <= 65533:
        raise ValueError('端口起点应为 1024 到 65533。')
    if vm_exists and previous is not None and port != previous:
        raise ValueError(f'此实例已使用端口 {previous}–{previous + 2}，请沿用原端口。')
    return port


def check_free_ports(port):
    for number in range(port, port + 3):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', number))
            except OSError as exc:
                raise RuntimeError(f'本机端口 {number} 被占用。新实例可通过 --port-base 选择另一组三个连续端口。') from exc


@contextmanager
def install_lock(root):
    folder = child_on_drive(root, root / 'state/wechat-pilot')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder/'save.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('正在保存视频或部署，请在当前作业结束后重试。') from None
        yield


def copy_file(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and filecmp.cmp(source, dest, shallow=False):
        return
    with tempfile.TemporaryDirectory(dir=dest.parent) as stage:
        pending = Path(stage)/dest.name
        shutil.copy2(source, pending)
        os.replace(pending, dest)


def install_host(interpreter):
    dest = RUNTIME / 'bin'
    dest.mkdir(exist_ok=True, parents=True)
    # Jobs and direct wechat-save invocations must use the same tools as the installer.
    with tempfile.TemporaryDirectory(dir=dest) as stage:
        pending = Path(stage) / 'wechat-python'
        pending.write_text(
            '#!/bin/sh\nexport WECHAT_DATA_ROOT=' + shlex.quote(str(APP)) +
            '\nexport WECHAT_PORT_BASE=' + shlex.quote(os.environ['WECHAT_PORT_BASE']) +
            '\nexport DOCKER_CONFIG=' + shlex.quote(str(RUNTIME / 'docker-config')) +
            '\nexport PATH=' + shlex.quote(str(dest)) + ':"$PATH"\nexec ' +
            shlex.quote(str(interpreter)) + ' "$@"\n')
        pending.chmod(0o755)
        os.replace(pending, dest / pending.name)
    for folder in ('scripts','tests'):
        for src in (SOURCE/folder).iterdir():
            if src.is_file() and not src.name.startswith('.'):
                copy_file(src, APP/folder/src.name)
    copy_file(SOURCE/'wechat-save', APP/'wechat-save')
    copy_file(SOURCE/'保存微信作品.md', APP/'保存微信作品.md')
    install_authors()


def install_authors():
    # Lima already shares state/. Keep the existing config path as an alias so
    # both the Mac saver and the Docker dashboard always read the same names.
    old = APP/'config/authors.txt'
    folder = APP/'state/monitoring'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    authors = folder/'authors.txt'
    with (folder/'authors.txt.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if old.is_symlink():
            if old.resolve() != authors or not authors.is_file():
                raise RuntimeError(f'作者名单链接指向意外位置或文件缺失：{old}')
            return
        if not authors.exists():
            if old.is_file():
                copy_file(old, authors)
            else:
                authors.touch(mode=0o600)
        elif old.is_file():
            saved = authors.read_text().splitlines()
            missing = [line for line in old.read_text().splitlines() if line.strip() and line not in saved]
            if missing:
                with tempfile.TemporaryDirectory(dir=folder) as stage:
                    pending = Path(stage)/authors.name
                    pending.write_text('\n'.join(saved+missing)+'\n')
                    os.replace(pending,authors)
        with tempfile.TemporaryDirectory(dir=old.parent) as stage:
            alias = Path(stage)/old.name
            alias.symlink_to(os.path.relpath(authors,old.parent))
            os.replace(alias,old)


def install_dashboard_files(deploy):
    runtime = APP/'state/dashboard-app'
    source = SOURCE/'dashboard'
    assets = {asset.relative_to(source): asset for asset in (
        source/'server.py', source/'account_search.py', source/'crawl_queue.py',
        source/'public_articles.py', source/'public_routes.py',
        source/'video_analysis_queue.py', source/'video_analysis_routes.py',
        *sorted(p for p in (source/'static').rglob('*') if p.is_file()),
    )}
    for module in ('wechat_settings.py', 'wechat_events.py', 'wechat_paths.py'):
        assets[Path(module)] = SOURCE/'scripts'/module
    revision = hashlib.sha256()
    for name, asset in assets.items():
        revision.update(name.as_posix().encode() + b'\0' + bytes.fromhex(digest(asset)))
        copy_file(asset, runtime/name)
    (APP/'state/posters').mkdir(mode=0o700, exist_ok=True)
    (APP/'media/微信视频号').mkdir(exist_ok=True)
    copy_file(source/'Dockerfile', deploy/'dashboard/Dockerfile')
    copy_file(SOURCE/'deploy/posters.py', APP/'deploy/posters.py')
    return revision.hexdigest()


def start_search_worker():
    monitoring = APP/'state/monitoring'
    requests, replies = monitoring/'search-requests', monitoring/'search-replies'
    for folder in (requests, replies):
        folder.mkdir(mode=0o700, exist_ok=True)
    # macOS grants this user's interactive deployment access to the removable
    # volume. Independent login agents require a separate disk access grant.
    worker = [RUNTIME/'bin/wechat-python', '-B', APP/'scripts/author_search_worker.py', '--serve']
    with (monitoring/'search-worker.log').open('ab') as log:
        process = subprocess.Popen(worker, cwd='/', start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, env=dict(os.environ, TMPDIR=str(APP/'tmp')))
    # An expired request exercises the actual shared queue without opening WeChat.
    key = uuid.uuid4().hex + '.json'
    request, reply = requests/key, replies/key
    try:
        with tempfile.TemporaryDirectory(dir=monitoring) as staging:
            pending = Path(staging)/key
            pending.write_text(json.dumps(dict(q='部署检查', expires=0)), encoding='utf-8')
            os.replace(pending, request)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if reply.is_file() and '超时' in json.loads(reply.read_text()).get('error', ''):
                return
            if process.poll() not in (None, 0):
                break
            time.sleep(.2)
        raise RuntimeError(f'视频号搜索进程启动检查失败，请查看 {monitoring}/search-worker.log。')
    finally:
        request.unlink(missing_ok=True)
        reply.unlink(missing_ok=True)


def start_crawl_worker():
    monitoring = APP/'state/monitoring'
    key = monitoring/'crawl-worker.key'
    if not key.exists():
        with tempfile.TemporaryDirectory(dir=monitoring) as stage:
            pending = Path(stage)/'key'
            pending.write_text(secrets.token_hex(32))
            pending.chmod(0o600)
            os.replace(pending, key)
    command = [RUNTIME/'bin/wechat-python', '-B', APP/'scripts/crawl_worker.py', '--serve']
    with (monitoring/'crawl-worker.log').open('ab') as log:
        child = subprocess.Popen(command, cwd='/', start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, env=dict(os.environ, TMPDIR=str(APP/'tmp')))
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and child.poll() in (None, 0):
        try:
            with opener.open(DASHBOARD+'api/crawls', timeout=3) as response:
                if json.load(response).get('worker_online'):
                    return
        except (ValueError, URLError, OSError):
            pass
        time.sleep(.4)
    raise RuntimeError(f'定时采集进程未就绪，请查看 {monitoring}/crawl-worker.log。')


def start_video_analysis_worker():
    monitoring = APP/'state/monitoring'
    key = monitoring/'video-analysis-worker.key'
    if not key.exists():
        with tempfile.TemporaryDirectory(dir=monitoring) as stage:
            pending = Path(stage)/key.name
            pending.write_text(secrets.token_hex(32))
            pending.chmod(0o600)
            # Preserve the active worker's credential if two deployments overlap.
            try:
                os.link(pending, key)
            except FileExistsError:
                pass
    for name in ('video_analysis_worker.py', 'video_analysis_probe.py'):
        copy_file(SOURCE/'scripts'/name, APP/'scripts'/name)
    command = [RUNTIME/'bin/wechat-python', '-B', APP/'scripts/video_analysis_worker.py']
    with (monitoring/'video-analysis-worker.log').open('ab') as log:
        subprocess.Popen(command, cwd='/', start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, env=dict(os.environ, TMPDIR=str(APP/'tmp')))


def start_vm():
    run(sys.executable, '-B', SOURCE/'deploy/bootstrap-runtime.py', '--root', APP)
    lima = RUNTIME/'bin/limactl'
    env = dict(os.environ, LIMA_HOME=str(lima_home(APP)))
    if not (lima_home(APP)/'wx/lima.yaml').is_file():
        prepare_vm_disk(APP)
        run(lima, 'create', '--tty=false', '--name=wx', RUNTIME/'wx.yaml', env=env)
    state = json.loads(output(lima, 'list', '--json', 'wx', env=env))
    if state['status'] != 'Running':
        place_vm_disk(APP)
        check_free_ports(int(os.environ['WECHAT_PORT_BASE']))
        run(lima, 'start', '--tty=false', 'wx', env=env)
    with (SOURCE/'deploy/guest-swap.sh').open('rb') as script:
        run(lima, 'shell', '--workdir=/', 'wx', '--', 'sudo', '-n', 'sh', '-s', stdin=script, env=env)
    for _ in range(30):
        if subprocess.run(DOCKER+['info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError('wx 虚拟机的 Docker 引擎未就绪。')


def prepare_image(env, component):
    spec = IMAGES[component]
    tag = spec['tag']
    archive = APP/'images'/spec['archive']
    if subprocess.run(DOCKER+['image','inspect',tag], capture_output=True).returncode == 0:
        return
    if archive.is_file():
        manifest = archive.with_suffix('.tar.sha256')
        checksum = spec['archive_sha256']
        if (not manifest.is_file() or manifest.read_text().strip() != checksum+'  '+archive.name
                or digest(archive) != checksum):
            raise RuntimeError(f'离线镜像校验失败，请检查 {manifest}。')
        run(*DOCKER, 'image', 'load', '--input', archive)
        if output(*DOCKER, 'image', 'inspect', '--format', '{{.Id}}', tag) != spec['archive_image_id']:
            raise RuntimeError('导入后的镜像与仓库记录的镜像 ID 不一致。')
    else:
        from oci_cache import prepare
        layout = prepare(spec['base'], APP / 'cache' / ('oci-' + component))
        context = RUNTIME / 'deployment' / ('docker' if component == 'player' else 'dashboard')
        # BuildKit reads the verified OCI blobs from this Mac, including across VM restarts.
        run(*DOCKER, 'buildx', 'build', '--platform', spec['platform'], '--load', '--tag', tag,
            '--build-arg', 'BASE_IMAGE=wm-verified-base',
            '--build-context', f'wm-verified-base=oci-layout://{layout}:base', context, env=env)
    run(*DOCKER, 'image', 'inspect', tag, stdout=subprocess.DEVNULL)


def existing_player():
    names = output(*DOCKER, 'container', 'ls', '-a', '--format', '{{.Names}}').splitlines()
    if 'wx-playback' not in names:
        return None
    obj = json.loads(output(*DOCKER, 'inspect', 'wx-playback'))[0]
    labels = obj['Config'].get('Labels', {})
    mounts = {v['Destination']:v for v in obj['Mounts']}
    profile, downloads = mounts.get('/config',{}), mounts.get('/config/Downloads',{})
    if (labels.get('com.docker.compose.project') != 'wechat-monitor'
            or labels.get('com.docker.compose.service') != 'weixin'
            or profile.get('Name') != 'wechat-monitor_weixin-profile'
            or downloads.get('Source') != str(APP/'media')):
        raise RuntimeError('同名容器的项目、登录卷或媒体目录与本部署不符。请核对 wx-playback。')
    ports = obj['HostConfig']['PortBindings']
    for guest, port in [('3000/tcp','33021'),('3001/tcp','33022')]:
        if ports.get(guest) != [{'HostIp':'127.0.0.1','HostPort':port}]:
            raise RuntimeError('现有播放器的端口映射与预期不符。')
    return obj['State']['Status']


def wait_for_desktop():
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        ready = subprocess.run(DOCKER+['exec','-u','abc','wx-playback','sh','-ec',GUEST_CHECK],
                               capture_output=True, timeout=12)
        if ready.returncode == 0:
            try:
                with opener.open(DESKTOP,timeout=2) as response:
                    if response.status == 200:
                        return
            except (URLError, OSError):
                pass
        time.sleep(1)
    raise RuntimeError('微信桌面、录制依赖或本机桌面入口没有就绪；请检查 wx-playback 日志。')


def dashboard_deployment():
    deploy = RUNTIME/'deployment'
    copy_file(SOURCE/'compose.yaml', deploy/'compose.yaml')
    compose = [*DOCKER, 'compose', '-f', str(deploy/'compose.yaml')]
    revision = install_dashboard_files(deploy)
    settings = dict(WECHAT_MEDIA_DIR=str(APP/'media'), WECHAT_STATE_DIR=str(APP/'state'),
                    DASHBOARD_UID=str(os.getuid()), DASHBOARD_REVISION=revision)
    # Compose loads this file even when invoked directly from another shell.
    with tempfile.TemporaryDirectory(dir=deploy) as stage:
        pending = Path(stage) / '.env'
        pending.write_text(''.join(f'{key}={json.dumps(value.replace("$", "$$"), ensure_ascii=False)}\n'
                                   for key, value in settings.items()))
        os.replace(pending, deploy / '.env')
    env = dict(os.environ, **settings)
    run(*compose, 'config', '--quiet', env=env)
    return deploy, compose, env


def start_dashboard(compose, env):
    run(*compose, 'up', '-d', '--no-build', '--no-deps', 'dashboard', env=env)
    opener = build_opener(ProxyHandler({}))
    for _ in range(30):
        try:
            with opener.open(DASHBOARD+'api/dashboard', timeout=2) as response:
                library = json.load(response)
            with opener.open(DASHBOARD+'api/public', timeout=2) as response:
                public = json.load(response)
            with opener.open(DASHBOARD+'api/settings', timeout=2) as response:
                storage = json.load(response).get('storage_root')
            if storage is not None and storage != str(APP / 'media'):
                raise RuntimeError('该端口对应其他数据目录的管理页，请核对本机端口。')
            if (isinstance(library.get('videos'), list) and isinstance(library.get('authors'), list)
                    and isinstance(public.get('accounts'), list) and storage == str(APP / 'media')):
                return
        except (URLError, OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('管理页未就绪，请检查 dashboard 容器及管理页端口。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video-analysis-only', action='store_true',
                        help='更新现有管理页并启动独立分析进程')
    parser.add_argument('--data-dir', type=Path, help='实例数据目录，支持本机磁盘和已挂载外盘')
    parser.add_argument('--port-base', type=int, default=os.getenv('WECHAT_PORT_BASE'),
                        help='本机三个连续端口的起点，默认 33021')
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.uname().machine != 'arm64':
        raise RuntimeError('本安装器适用于 Apple Silicon macOS。')
    root = resolve_root(args.data_dir)
    check_drive(root, initialize=not args.video_analysis_only)
    with install_lock(root):
        port = port_base(root, args.port_base)
        configure(root, port)
        check_install_paths()
        for folder in ('media', 'state', 'tmp', 'config'):
            (APP / folder).mkdir(mode=0o700, exist_ok=True)
        from host_tools import prepare_host
        if args.video_analysis_only:
            if not SOCKET.is_socket() or not (RUNTIME/'bin/wechat-python').is_file():
                raise RuntimeError('请先完成管理页的一键部署。')
            prepare_host(APP)
            _, compose, env = dashboard_deployment()
            prepare_image(env, 'dashboard')
            start_dashboard(compose, env)
            start_video_analysis_worker()
            print('视频分析管理页：' + DASHBOARD)
            return
        if not SOCKET.is_socket():
            check_free_ports(port)
        port_file = APP / 'config/port-base'
        if not port_file.exists() or port_file.read_text().strip() != str(port):
            with tempfile.TemporaryDirectory(dir=port_file.parent) as stage:
                pending = Path(stage) / 'ports'
                pending.write_text(str(port) + '\n')
                os.replace(pending, port_file)
        interpreter = prepare_host(APP)
        print('正在准备此实例的 Docker 引擎。', flush=True)
        start_vm()
        deploy, compose, env = dashboard_deployment()
        copy_file(SOURCE/'docker/Dockerfile',deploy/'docker/Dockerfile')
        status = existing_player()
        prepare_image(env, 'player')
        prepare_image(env, 'dashboard')
        print('正在检查播放器镜像，安装保存工具和 OCR。', flush=True)
        install_host(interpreter)
        if status is None:
            run(*compose,'up','-d','--no-build','weixin',env=env)
        elif status in ('created','exited'):
            run(*DOCKER,'start','wx-playback')
        elif status != 'running':
            raise RuntimeError('现有播放器状态异常：' + status)
        previews = subprocess.run([sys.executable, '-B', str(APP/'deploy/posters.py')])
        if previews.returncode:
            print('部分视频暂时没有封面，仍可在管理页查看作品。', file=sys.stderr)
        start_dashboard(compose, env)
        wait_for_desktop()
        try:
            start_video_analysis_worker()
        except OSError as exc:
            print(f'视频分析进程启动失败：{exc}', file=sys.stderr)
        start_search_worker()
        start_crawl_worker()
        write_event(APP/'state/monitoring', 'system', 'info', '管理台部署完成，采集和账号搜索进程已就绪。')
        save_selection(APP)
        print('数据目录：' + str(APP))
        print('视频号管理页：' + DASHBOARD)
        print('微信桌面已就绪：' + DESKTOP)
        print('保存命令：' + str(APP/'wechat-save') + ' save <微信分享链接>')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit('部署未完成：'+str(exc))
