"""Settings and indexed video paths shared by the Mac tools and dashboard."""
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import urlsplit


STORAGE_ROOT = Path('/Volumes/ssd1/wechat-monitor/media')
DEFAULT_STORAGE_PATH = STORAGE_ROOT / '微信视频号'
SETTINGS_FILENAME = 'settings.json'


class SettingsError(ValueError):
    pass


def read_settings(path, *, default_storage_path=DEFAULT_STORAGE_PATH):
    defaults = dict(storage_path=str(default_storage_path), model_api_url='', model_name='', model_api_key='')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return defaults
    except (ValueError, RecursionError):
        raise SettingsError('配置文件格式无效。') from None
    if not isinstance(data, dict) or any(not isinstance(data.get(key, value), str) for key, value in defaults.items()):
        raise SettingsError('配置文件格式无效。')
    return {key: data.get(key, value) for key, value in defaults.items()}


def storage_relative(value, host_root, *, allow_root=False):
    error = '请输入媒体根目录下的绝对路径。'
    if not isinstance(value, str) or len(value) > 1024 or not value.isprintable():
        raise ValueError(error)
    path = Path(value)
    if (not path.is_absolute() or not path.is_relative_to(host_root) or '..' in path.parts
            or (path == host_root and not allow_root)):
        raise ValueError(error)
    relative = path.relative_to(host_root)
    if any(len(part.encode('utf-8')) > 255 for part in relative.parts):
        raise ValueError('保存目录的单级名称过长。')
    return relative


def resolve_storage_path(value, host_root, local_root):
    """Translate the Mac path into this process's view of the media mount."""
    relative = storage_relative(value, host_root)
    directory = local_root.resolve(strict=True)
    device = directory.stat().st_dev
    for part in relative.parts:
        directory = directory / part
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue  # The saver can create this directory on its next run.
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
            raise ValueError('保存目录必须是媒体根目录里的普通目录。')
    return directory


def child_directories(local_root, relative):
    # Open each child relative to its parent descriptor so a concurrent symlink
    # replacement cannot redirect the browser outside the mounted directory.
    flags = os.O_RDONLY | os.O_DIRECTORY
    owner = os.open(local_root, flags)
    try:
        device = os.fstat(owner).st_dev
        for name in relative.parts:
            try:
                child = os.open(name, flags | os.O_NOFOLLOW, dir_fd=owner)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError('请选择媒体根目录内的普通文件夹。') from None
                raise
            os.close(owner)
            owner = child
            if os.fstat(owner).st_dev != device:
                raise ValueError('请选择媒体根目录内的普通文件夹。')
        names = []
        with os.scandir(owner) as entries:
            for entry in entries:
                if entry.name.startswith('.') or not entry.name.isprintable() or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and info.st_dev == device:
                        names.append(entry.name)
                except FileNotFoundError:
                    continue
        return sorted(names, key=lambda name: (name.casefold(), name))
    finally:
        os.close(owner)


def indexed_path(root, legacy, value):
    """A leading './' marks a path relative to the media mount. Older rows use the default directory."""
    custom = value.startswith('./')
    if custom:
        value = value[2:]
    relative = Path(value)
    if not relative.parts or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('录像索引路径无效。')
    try:
        mount = root.resolve()
        base = mount if custom else legacy.resolve()
        path = (base / relative).resolve()
    except RuntimeError:
        raise ValueError('录像索引路径无效。') from None
    if not base.is_relative_to(mount) or not path.is_relative_to(base):
        raise ValueError('录像索引路径无效。')
    return path


def model_text(value, label, maximum):
    if not isinstance(value, str) or len(value) > maximum or (value and not value.isprintable()):
        raise ValueError(f'{label}必须是最多 {maximum} 个字符的单行文本。')
    return value.strip()


def model_url(value):
    value = model_text(value, '模型 API URL', 1024)
    if value:
        try:
            parts = urlsplit(value)
            if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.port == 0
                    or parts.username is not None or any(c.isspace() or c in '\\?#' for c in value)):
                raise ValueError
        except ValueError:
            raise ValueError('请输入 HTTP 或 HTTPS 模型 API 地址，去掉凭据、查询参数和片段。') from None
    return value


class SettingsStore:
    def __init__(self, path, host_root, local_root):
        self.path = path
        self.host_root = host_root
        self.local_root = local_root
        self.default_storage_path = host_root / DEFAULT_STORAGE_PATH.name

    def read(self):
        return read_settings(self.path, default_storage_path=self.default_storage_path)

    def directories(self, value=None):
        if self.local_root is None:
            raise OSError('Media root is not mounted')
        relative = storage_relative(str(self.host_root) if value is None else value, self.host_root, allow_root=True)
        path = self.host_root / relative
        names = child_directories(self.local_root, relative)
        selectable = path != self.host_root
        return dict(path=str(path), root=str(self.host_root), parent=str(path.parent) if selectable else None,
                    directories=names, can_select=selectable)

    def public(self):
        data = self.read()
        data['model_api_key_configured'] = bool(data.pop('model_api_key'))
        data['storage_root'] = str(self.host_root)
        try:
            data['storage_available_bytes'] = shutil.disk_usage(self.local_root).free if self.local_root else None
        except OSError:
            data['storage_available_bytes'] = None
        return data

    def update(self, fields):
        if not fields or fields.keys() - {'storage_path', 'model_api_url', 'model_name', 'model_api_key'}:
            raise ValueError('请提供需要更改的配置项。')
        changes = fields.copy()
        if 'storage_path' in changes:
            if self.local_root is None:
                raise ValueError('当前只挂载了原视频目录，升级部署后可修改保存目录。')
            value = changes['storage_path']
            resolve_storage_path(value, self.host_root, self.local_root)
            changes['storage_path'] = str(Path(value))
        if 'model_api_url' in changes:
            changes['model_api_url'] = model_url(changes['model_api_url'])
        if 'model_name' in changes:
            changes['model_name'] = model_text(changes['model_name'], '模型名称', 200)
        if 'model_api_key' in changes:
            value = changes['model_api_key']
            if not isinstance(value, str) or not re.fullmatch(r'[!-~]{0,1024}', value):
                raise ValueError('API Key 最多 1024 个字符，且不能含空白字符。')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self.read()
            data.update(changes)
            with tempfile.TemporaryDirectory(dir=self.path.parent) as staging:
                pending = Path(staging) / self.path.name
                with pending.open('w', encoding='utf-8') as out:
                    os.fchmod(out.fileno(), 0o600)
                    json.dump(data, out, ensure_ascii=False)
                    out.write('\n')
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(pending, self.path)
            return self.public()
