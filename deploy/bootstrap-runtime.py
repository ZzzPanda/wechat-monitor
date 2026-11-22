#!/usr/bin/env python3
"""Prepare the pinned Lima runtime and Docker guest in the selected project directory."""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_tools import DownloadError, download_release

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_paths import check_drive, child_on_drive, lima_home, resolve_root

def github_release(name, repo, release, asset_id, size, sha):
    urls = (f'https://github.com/{repo}/releases/download/{release}',
            f'https://api.github.com/repos/{repo}/releases/assets/{asset_id}')
    return name, urls, size, sha


LIMA_TAR = github_release('lima-verified.tgz', 'lima-vm/lima', 'v2.3.0/lima-2.3.0-Darwin-arm64.tar.gz',
                          717479690, 50317981, 'b7af444bd8f91d3c307477e562f2cf3bd7595d62b9429185e4fa841563578cfc')
# The upstream 2.3.0 Darwin binary targets macOS 26. Homebrew also publishes
# a relocatable arm64 Sequoia build of the same release, including its guest agent.
SEQUOIA_SHA = 'c50b922d11d6d9b3ca799bca38f185600c9d6c662ad3516e15759f4514e2789a'
LIMA_SEQUOIA = ('lima-2.3.0-arm64-sequoia.tgz',
                ('https://ghcr.io/v2/homebrew/core/lima/blobs/sha256:' + SEQUOIA_SHA,), 50498152, SEQUOIA_SHA)
GUEST_GZ = github_release('guest.raw.gz', 'abiosoft/colima-core',
            'v0.10.4/ubuntu-24.04-minimal-cloudimg-arm64-docker.raw.gz',
            437242106, 332354401, '1fc0354f4f99734ce3886628cc7af8b0437c1a1d391b126bd09cba0df35ee53f')
GUEST_BYTES = 3758096384
GUEST_SHA = '7ab64981326b7c34f4822d58764e83d2b4cb097472fa1c23975f3bb7b53790d0'
LIMACTL_SHA = '807fe2112a5524392153494f07b989eccd9f7c92bb45053966a2e83816631d3b'
SEQUOIA_LIMACTL_SHA = '1ad6e13af64b76b09a9ae293bcbf6968e0da4970b9c1eb39f8e805292439ebc9'
BLOCK = 1024 * 1024


def digest(path):
    with path.open('rb') as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(BLOCK), b''):
            h.update(block)
        return h.hexdigest()


def volume_dir(path, parent):
    dest = child_on_drive(parent, path)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dest


def artifact(cache, source):
    name, urls, size, sha = source
    target = cache / name
    # prepare() serializes these stable partials across installer restarts.
    pending = cache / ('.' + name + '.' + sha[:12] + '.part')
    for path in (target, pending):
        if path.is_symlink() or path.exists() and not path.is_file():
            raise ValueError(f'缓存路径必须是普通文件：{path}')
    return download_release({'name': name, 'size': size, 'sha256': sha}, urls, target, pending)


def unpack_lima(archive, tree, bottle=False):
    allowed = {'bin', 'libexec', 'share'}
    if bottle:
        allowed.update(('LICENSE', 'NOTICE'))
    prefix = PurePosixPath('lima/2.3.0')
    with tarfile.open(archive, 'r:gz') as tar:
        for entry in tar:
            rel = PurePosixPath(entry.name)
            if bottle:
                if not rel.is_relative_to(prefix):
                    continue
                rel = rel.relative_to(prefix)
                if not rel.parts or rel.parts[0] not in allowed:
                    continue
            if rel == PurePosixPath('.') and entry.isdir():
                continue
            if not rel.parts or rel.is_absolute() or '..' in rel.parts or rel.parts[0] not in allowed:
                raise ValueError(f'不支持的归档路径：{entry.name}')
            dest = tree / rel
            if entry.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open('xb') as out, tar.extractfile(entry) as src:
                    shutil.copyfileobj(src, out, BLOCK)
                dest.chmod(0o755 if entry.mode & 0o111 else 0o644)
            elif (entry.issym() and str(rel) == 'share/doc/lima/templates'
                  and entry.linkname == '../../lima/templates'):
                # This documentation shortcut is optional. Never extract archive links.
                continue
            else:
                raise ValueError(f'不支持的归档条目：{entry.name}')


def install_lima(runtime, cache):
    sequoia = int(platform.mac_ver()[0].split('.')[0]) < 26
    expected = SEQUOIA_LIMACTL_SHA if sequoia else LIMACTL_SHA
    bindir = volume_dir(runtime / 'bin', runtime)
    command = bindir / 'limactl'
    if command.is_file() and os.access(command, os.X_OK):
        installed = digest(command) if command.resolve().is_relative_to(runtime) else None
        if installed not in (SEQUOIA_LIMACTL_SHA, expected):
            raise ValueError(f'已安装的 limactl 路径或校验和不正确：{command}')
        return
    if os.path.lexists(command):
        raise ValueError(f'现有 limactl 无法执行：{command}')
    candidates = [(LIMA_SEQUOIA, runtime / 'lima-2.3.0-sequoia', SEQUOIA_LIMACTL_SHA)]
    if not sequoia:
        candidates.insert(0, (LIMA_TAR, runtime / 'lima-2.3.0', LIMACTL_SHA))
    # An interrupted install may already have unpacked a verified package of either build.
    staged = [candidate for candidate in candidates if os.path.lexists(candidate[1])]
    for source, package, expected in staged or candidates:
        if package.is_symlink():
            raise ValueError(f'软件包目录不可为符号链接：{package}')
        if not package.exists():
            try:
                archive = artifact(cache, source)
            except DownloadError:
                if source != LIMA_TAR:
                    raise
                print('GitHub 发行包暂时不可用，改用相同版本的 Homebrew ARM64 发行包。', flush=True)
                continue
            with tempfile.TemporaryDirectory(dir=runtime) as work:
                tree = Path(work) / 'package'
                tree.mkdir()
                unpack_lima(archive, tree, bottle=source == LIMA_SEQUOIA)
                if digest(tree / 'bin/limactl') != expected:
                    raise ValueError('limactl 解包校验失败')
                os.rename(tree, package)
        binary = package / 'bin/limactl'
        if not binary.is_file() or not binary.resolve().is_relative_to(package) or digest(binary) != expected:
            raise ValueError(f'已展开的 Lima 工具未通过校验：{binary}')
        command.symlink_to(Path('..') / package.name / 'bin/limactl')
        return


def install_guest(cache):
    target = cache / 'guest.raw'
    if target.is_symlink():
        raise ValueError(f'基础盘不可为符号链接：{target}')
    if target.exists():
        if not target.is_file():
            raise ValueError(f'基础镜像路径不是文件：{target}')
        if target.stat().st_size == GUEST_BYTES and digest(target) == GUEST_SHA:
            return target
        print('基础镜像缓存不完整，将从已校验的压缩包重新生成。', flush=True)
    archive = artifact(cache, GUEST_GZ)
    with tempfile.TemporaryDirectory(dir=cache) as work:
        pending = Path(work) / 'guest.raw'
        with gzip.open(archive, 'rb') as src, pending.open('xb') as out:
            h = hashlib.sha256()
            for block in iter(lambda: src.read(BLOCK), b''):
                out.write(block)
                if out.tell() > GUEST_BYTES:
                    raise ValueError('解压后的基础镜像超出预期长度')
                h.update(block)
            if out.tell() != GUEST_BYTES or h.hexdigest() != GUEST_SHA:
                raise ValueError('基础镜像解压校验失败')
        os.replace(pending, target)
    return target


def prepare(root):
    root = resolve_root(root)
    check_drive(root, initialize=True)
    lock = os.open(root / '.bootstrap-runtime.lock', os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock, 'r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        runtime = volume_dir(root / 'runtime', root)
        cache = volume_dir(root / 'cache', root)
        install_lima(runtime, cache)
        instance = lima_home(root) / 'wx'
        if instance.exists():
            if not (instance / 'lima.yaml').is_file():
                raise ValueError(f'已有实例目录缺少 lima.yaml，请先检查：{instance}')
            print('复用现有 wx 虚拟机。')
            return
        guest = install_guest(cache)
        config = runtime / 'wx.yaml'
        if config.is_symlink():
            raise ValueError(f'虚拟机模板不可为符号链接：{config}')
        template = Path(__file__).with_name('lima.yaml').read_text()
        for marker, path in [('@GUEST@', guest), ('@MEDIA@', root / 'media'), ('@STATE@', root / 'state')]:
            template = template.replace(marker, json.dumps(str(path), ensure_ascii=False))
        base = int(os.environ.get('WECHAT_PORT_BASE', '33021'))
        if not 1024 <= base <= 65533:
            raise ValueError('本地起始端口必须在 1024 到 65533 之间。')
        for offset, marker in enumerate(('@DESKTOP_PORT@', '@DESKTOP_TLS_PORT@', '@DASHBOARD_PORT@')):
            template = template.replace(marker, str(base + offset))
        # An interrupted first run may have staged a template before a VM existed.
        with tempfile.TemporaryDirectory(dir=runtime) as work:
            pending = Path(work) / 'wx.yaml'
            pending.write_text(template)
            os.replace(pending, config)
        print(f'Lima 和 Docker 基础镜像已就绪：{config}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=None)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        prepare(args.root)
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        sys.exit(str(exc))
