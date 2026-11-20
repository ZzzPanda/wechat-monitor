#!/usr/bin/env python3
"""Prepare the pinned Lima runtime and Docker guest on the project's external SSD."""
import argparse
import fcntl
import gzip
import hashlib
from http.client import HTTPException
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

VOLUME = Path('/Volumes/ssd1')
VOLUME_UUID = '854330B0-E6A8-4BB0-8D7D-767BBEA0AF36'
LIMA_REPO = 'lima-vm/lima'
GUEST_REPO = 'abiosoft/colima-core'
LIMA_TAR = ('lima-verified.tgz', LIMA_REPO, 'v2.3.0/lima-2.3.0-Darwin-arm64.tar.gz',
            717479690, 50317981, 'b7af444bd8f91d3c307477e562f2cf3bd7595d62b9429185e4fa841563578cfc')
GUEST_GZ = ('guest.raw.gz', GUEST_REPO,
            'v0.10.4/ubuntu-24.04-minimal-cloudimg-arm64-docker.raw.gz',
            437242106, 332354401, '1fc0354f4f99734ce3886628cc7af8b0437c1a1d391b126bd09cba0df35ee53f')
GUEST_BYTES = 3758096384
GUEST_SHA = '7ab64981326b7c34f4822d58764e83d2b4cb097472fa1c23975f3bb7b53790d0'
LIMACTL_SHA = '807fe2112a5524392153494f07b989eccd9f7c92bb45053966a2e83816631d3b'
BLOCK = 1024 * 1024


def digest(path):
    with path.open('rb') as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(BLOCK), b''):
            h.update(block)
        return h.hexdigest()


def volume_dir(path, parent=VOLUME):
    """Create missing children only after proving their physical parent is on the SSD."""
    path = path.resolve()
    if path == parent or not path.is_relative_to(parent):
        raise ValueError(f'预期指定外盘的子目录：{path}')
    existing = next(p for p in (path, *path.parents) if p.exists())
    if existing.stat().st_dev != VOLUME.stat().st_dev:
        raise ValueError(f'目录位于另一块磁盘：{existing}')
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact(cache, source):
    name, repo, release, asset_id, size, sha = source
    target = cache / name
    if target.is_symlink():
        raise ValueError(f'缓存文件不可为符号链接：{target}')
    if target.is_file() and target.stat().st_size == size and digest(target) == sha:
        return target
    print(f'获取 {name}（{size // 1024**2} MiB）', flush=True)
    urls = [f'https://github.com/{repo}/releases/download/{release}',
            f'https://api.github.com/repos/{repo}/releases/assets/{asset_id}']
    errors = []
    for url in urls:
        try:
            request = Request(url, headers={'User-Agent': 'wechat-monitor-deploy',
                                            'Accept': 'application/octet-stream'})
            with tempfile.TemporaryDirectory(dir=cache) as work:
                pending = Path(work) / name
                with urlopen(request, timeout=90) as response, pending.open('xb') as out:
                    if not response.geturl().startswith('https://'):
                        raise ValueError('下载地址未使用 HTTPS')
                    h = hashlib.sha256()
                    for block in iter(lambda: response.read(BLOCK), b''):
                        out.write(block)
                        if out.tell() > size:
                            raise ValueError('下载长度超过发行文件')
                        h.update(block)
                    if out.tell() != size or h.hexdigest() != sha:
                        raise ValueError('发行文件的长度或 SHA-256 不匹配')
                os.replace(pending, target)
                return target
        except (OSError, ValueError, HTTPException) as exc:
            errors.append(str(exc))
    raise RuntimeError(f'{name} 获取失败：' + ' / '.join(errors))


def unpack_lima(archive, tree):
    with tarfile.open(archive, 'r:gz') as tar:
        for entry in tar:
            rel = PurePosixPath(entry.name)
            if rel == PurePosixPath('.') and entry.isdir():
                continue
            if not rel.parts or rel.is_absolute() or '..' in rel.parts or rel.parts[0] not in ('bin', 'libexec', 'share'):
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
    bindir = volume_dir(runtime / 'bin', runtime)
    command = bindir / 'limactl'
    if command.is_file() and os.access(command, os.X_OK):
        if not command.resolve().is_relative_to(VOLUME):
            raise ValueError(f'limactl 未安装在指定外盘：{command}')
        return
    if os.path.lexists(command):
        raise ValueError(f'现有 limactl 无法执行：{command}')
    package = runtime / 'lima-2.3.0'
    if package.is_symlink():
        raise ValueError(f'软件包目录不可为符号链接：{package}')
    if not package.exists():
        archive = artifact(cache, LIMA_TAR)
        with tempfile.TemporaryDirectory(dir=runtime) as work:
            tree = Path(work) / 'package'
            tree.mkdir()
            unpack_lima(archive, tree)
            if digest(tree / 'bin/limactl') != LIMACTL_SHA:
                raise ValueError('limactl 解包校验失败')
            os.rename(tree, package)
    binary = package / 'bin/limactl'
    if not binary.is_file() or not binary.resolve().is_relative_to(package) or digest(binary) != LIMACTL_SHA:
        raise ValueError(f'已展开的 Lima 工具未通过校验：{binary}')
    command.symlink_to(Path('..') / package.name / 'bin/limactl')


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
    disk = plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', str(VOLUME)]))
    if disk.get('VolumeUUID') != VOLUME_UUID or disk.get('MountPoint') != str(VOLUME):
        raise ValueError('请挂载指定的 1 TB 外置 SSD。')
    root = volume_dir(root)
    lock = os.open(root / '.bootstrap-runtime.lock', os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock, 'r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        runtime = volume_dir(root / 'runtime', root)
        cache = volume_dir(root / 'cache', root)
        install_lima(runtime, cache)
        instance = root / 'lima/wx'
        if instance.exists():
            if not (instance / 'lima.yaml').is_file():
                raise ValueError(f'已有实例目录缺少 lima.yaml，请先检查：{instance}')
            print('复用现有 wx 虚拟机。')
            return
        guest = install_guest(cache)
        config = runtime / 'wx.yaml'
        if config.is_symlink():
            raise ValueError(f'虚拟机模板不可为符号链接：{config}')
        if not config.exists():
            template = Path(__file__).with_name('lima.yaml').read_text()
            for marker, path in [('@GUEST@', guest), ('@MEDIA@', root / 'media'), ('@STATE@', root / 'state')]:
                template = template.replace(marker, json.dumps(str(path), ensure_ascii=False))
            with tempfile.TemporaryDirectory(dir=runtime) as work:
                pending = Path(work) / 'wx.yaml'
                pending.write_text(template)
                os.link(pending, config)
        print(f'Lima 和 Docker 基础镜像已就绪：{config}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=VOLUME / 'wechat-monitor')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        prepare(args.root)
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        sys.exit(str(exc))
