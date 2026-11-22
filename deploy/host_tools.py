"""Install verified macOS arm64 tools into this instance without system packages."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / 'host-tools.lock.json').read_text())
WHEELS = ('numpy', 'pillow', 'pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-Quartz',
          'pyobjc-framework-CoreML', 'pyobjc-framework-Vision')


def valid(path, spec):
    if not path.is_file() or path.stat().st_size != spec['size']:
        return False
    with path.open('rb') as source:
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
        return digest.hexdigest() == spec['sha256']


class DownloadError(RuntimeError):
    pass


def download_release(spec, urls, target, pending):
    if valid(target, spec):
        return target
    if pending.is_file() and pending.stat().st_size >= spec['size']:
        if valid(pending, spec):
            os.replace(pending, target)
            return target
        pending.unlink()
    print(f"正在下载 {spec['name']}（{spec['size'] / 1e6:.1f} MB）…", flush=True)
    for url in urls:
        for attempt in range(3):
            resume = ['--continue-at', '-'] if pending.is_file() and pending.stat().st_size else []
            # Disable user curlrc options. curl drops Authorization on cross-host redirects by default.
            auth = ['-H', 'Authorization: Bearer QQ=='] if url.startswith('https://ghcr.io/v2/homebrew/') else []
            command = ['/usr/bin/curl', '--disable', '--fail', '--location', '--silent', '--show-error',
                       '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '20',
                       '--retry', '2', '--speed-limit', '512', '--speed-time', '60',
                       '--max-filesize', str(spec['size']), *resume, *auth, '-H',
                       'Accept: application/octet-stream', '-o', str(pending), url]
            result = subprocess.run(command)
            if result.returncode == 0:
                if valid(pending, spec):
                    os.replace(pending, target)
                    return target
                pending.unlink(missing_ok=True)
                break  # Complete response with wrong digest: try the alternate source.
            if resume and result.returncode in (22, 33, 36):
                pending.unlink(missing_ok=True)  # Server rejected the range; restart from byte zero.
            elif result.returncode in (6, 7, 18, 28, 35, 52, 55, 56, 92):
                if attempt < 2:
                    time.sleep(2 ** attempt)
            else:
                break
    raise DownloadError(f"{spec['name']} 下载或 SHA-256 校验失败，已验证的其他组件可在重试时复用。")


def fetch(name, cache):
    spec = LOCK[name]
    target = cache / spec['name']
    with tempfile.TemporaryDirectory(dir=cache) as stage:
        return download_release(spec, [url for url in (spec['url'], spec.get('fallback')) if url],
                                target, Path(stage) / 'download')


def install_tool(name, cache, tools):
    spec = LOCK[name]
    dest = tools / (name + '-' + spec['sha256'][:12])
    if not dest.is_dir():
        artifact = fetch(name, cache)
        with tempfile.TemporaryDirectory(dir=tools) as stage:
            tree = Path(stage) / 'files'
            tree.mkdir()
            if name in ('compose', 'buildx'):
                binary = tree / ('docker-' + name)
                shutil.copyfile(artifact, binary)
                binary.chmod(0o755)
            else:
                # All archives have immutable SHA-256 values in the release manifest.
                subprocess.run(['/usr/bin/tar', '-xf', str(artifact), '-C', str(tree)], check=True)
            tree.rename(dest)
    return dest


def link(binary, location):
    if location.is_symlink() and location.resolve() == binary.resolve():
        return
    with tempfile.TemporaryDirectory(dir=location.parent) as tmp:
        pending = Path(tmp) / location.name
        pending.symlink_to(binary)
        os.replace(pending, location)


def install_python_env(cache, tools):
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('请使用 ./deploy.sh 启动随项目固定的 Python 3.12。')
    version = hashlib.sha256(''.join(LOCK[name]['sha256'] for name in ('python', *WHEELS)).encode()).hexdigest()[:12]
    dest = tools / ('python-' + version)
    python = dest / 'bin/python3'
    if not (dest / '.ready').is_file() or not python.is_file():
        wheels = [fetch(name, cache) for name in WHEELS]
        subprocess.run([sys.executable, '-Im', 'venv', '--without-pip', str(dest)], check=True)
        subprocess.run([sys.executable, '-Im', 'pip', '--isolated', '--disable-pip-version-check',
                        '--python', str(python), 'install', '--no-index', '--no-deps', '--no-compile',
                        '--no-cache-dir', *wheels], check=True)
        subprocess.run([python, '-IBc', 'import numpy, PIL.Image, Quartz, Vision; assert Vision.VNRecognizeTextRequest'], check=True)
        (dest / '.ready').touch()
    return python


def prepare_host(root):
    """Return the worker interpreter. Child processes inherit only this setup's Docker config."""
    root = Path(root)
    cache = Path(os.environ.get('WECHAT_HOST_CACHE', Path.home() / 'Library/Caches/wechat-monitor'))
    runtime = root / 'runtime'
    tools, bindir = runtime / 'host-tools', runtime / 'bin'
    docker_config = runtime / 'docker-config'
    plugins = docker_config / 'cli-plugins'
    for directory in (cache, tools, bindir, plugins):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ['PATH'] = os.pathsep.join((str(bindir), os.environ.get('PATH', os.defpath)))
    os.environ['DOCKER_CONFIG'] = str(docker_config)
    # Each verified bundle is installed into its own versioned directory.
    with ThreadPoolExecutor(max_workers=3) as downloads:
        interpreter = downloads.submit(install_python_env, cache, tools)
        packages = {name: downloads.submit(install_tool, name, cache, tools)
                    for name in ('chrome', 'buildx', 'docker', 'compose', 'ffmpeg')}
        python = interpreter.result()
        installed = {name: task.result() for name, task in packages.items()}
    docker = installed['docker'] / 'bin/docker'
    link(docker, bindir / 'docker')
    for name in ('compose', 'buildx'):
        link(installed[name] / ('docker-' + name), plugins / ('docker-' + name))
    ffmpeg = installed['ffmpeg'] / LOCK['ffmpeg']['name'].removesuffix('.tar.xz') / 'bin'
    for name in ('ffmpeg', 'ffprobe'):
        link(ffmpeg / name, bindir / name)
    shell = installed['chrome'] / 'chrome-headless-shell-mac-arm64/chrome-headless-shell'
    # Headless Shell locates ICU and its resources relative to argv[0] on macOS.
    # Executing a symlink directly makes it search for them in runtime/bin.
    with tempfile.TemporaryDirectory(dir=bindir) as tmp:
        launcher = Path(tmp) / 'chromium'
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(str(shell))} "$@"\n')
        launcher.chmod(0o755)
        os.replace(launcher, bindir / 'chromium')
    link(bindir / 'chromium', bindir / 'google-chrome')
    with tempfile.TemporaryDirectory(dir=bindir) as tmp:
        script = Path(tmp) / 'vision-ocr.py'
        shutil.copyfile(HERE.parent / 'scripts/wechat_vision.py', script)
        os.replace(script, bindir / script.name)
        wrapper = Path(tmp) / 'wechat-visible-text-stdin'
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(str(python))} -IB {shlex.quote(str(bindir / script.name))} "$@"\n')
        wrapper.chmod(0o755)
        os.replace(wrapper, bindir / wrapper.name)
    for command in ([docker, 'compose', 'version'], [docker, 'buildx', 'version'], [bindir / 'ffprobe', '-version']):
        subprocess.run(command, stdout=subprocess.DEVNULL, check=True, timeout=30)
    ffmpeg_help = subprocess.check_output([bindir / 'ffmpeg', '-hide_banner', '-h', 'bsf=setts'],
                                          stderr=subprocess.STDOUT, text=True, timeout=30)
    if 'prescale' not in ffmpeg_help:
        raise RuntimeError('安装的 FFmpeg 缺少录制所需的 setts/prescale。')
    # --version alone succeeds even when Chrome cannot locate its ICU resource bundle.
    with tempfile.TemporaryDirectory(dir=runtime) as profile:
        dom = subprocess.check_output([bindir / 'chromium', '--headless', '--disable-gpu', '--disable-extensions',
                                       f'--user-data-dir={profile}', '--dump-dom',
                                       'data:text/html,<title>wechat-monitor</title>'], stderr=subprocess.PIPE, timeout=30)
        if b'<title>wechat-monitor</title>' not in dom:
            raise RuntimeError('浏览器未能读取本地测试页。')
    return python
