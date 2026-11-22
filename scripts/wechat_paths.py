"""Persistent paths shared by the installer and the Mac workers."""
import errno
import filecmp
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile


LEGACY_ROOT = Path('/Volumes/ssd1/wechat-monitor')
LEGACY_VOLUME_UUID = '854330B0-E6A8-4BB0-8D7D-767BBEA0AF36'
MARKER = '.wechat-monitor-volume'


def location_file():
    config = os.getenv('WECHAT_CONFIG_DIR') or Path.home() / '.config/wechat-monitor'
    return Path(config).expanduser() / 'location'


def saved_location():
    try:
        lines = location_file().read_text().splitlines()
    except FileNotFoundError:
        return None
    if len(lines) != 2 or not Path(lines[0]).is_absolute() or not lines[1]:
        raise ValueError(f'安装位置记录无效：{location_file()}')
    return Path(lines[0]), lines[1]


def legacy_install(root):
    return root == LEGACY_ROOT and (root / 'lima/wx/lima.yaml').is_file()


def resolve_root(explicit=None):
    selected = explicit or os.getenv('WECHAT_DATA_ROOT')
    if selected:
        root = Path(selected).expanduser()
        if not root.is_absolute() or not str(root).isprintable():
            raise ValueError('请提供绝对路径作为数据目录。')
        return root.resolve()
    installed = Path(__file__).resolve().parents[1]
    if (installed / MARKER).is_file() or legacy_install(installed):
        return installed
    selected = saved_location()
    if selected:
        return selected[0]
    return LEGACY_ROOT if legacy_install(LEGACY_ROOT) else Path.home() / '.wechat-monitor'


def socket_path_ok(home):
    longest = home / 'wx' / 'ssh.sock.1234567890123456'
    return len(os.fsencode(longest)) < 104 and all(c.isalnum() or c in '/-._' for c in str(home))


def lima_home(root):
    # Lima resolves symlinks and uses Unix sockets with a short path limit on macOS.
    local = root / 'lima'
    intent = root / 'vm/.instance'
    if intent.is_symlink() or (root / 'vm').is_symlink():
        raise RuntimeError(f'虚拟机位置记录不应为符号链接：{intent}')
    # Preserve existing instances, including paths that would be shortened for a new VM.
    if not intent.exists() and ((local / 'wx').exists() or socket_path_ok(local)):
        return local
    marker = root / MARKER
    identity = marker.read_text().strip() if marker.is_file() and not marker.is_symlink() else ''
    name = hashlib.sha256(os.fsencode(root) + b'\0' + identity.encode()).hexdigest()[:16]
    private = Path.home() / '.wm-vms' / name
    shared = Path('/Users/Shared') / f'.wm-vms-{os.getuid()}' / name
    if intent.exists():
        registered = Path(intent.read_text().strip())
        if registered not in (private, shared):
            raise RuntimeError(f'已登记的虚拟机位置与本次路径不同，请恢复原数据目录和用户：{intent}')
        return registered
    return private if socket_path_ok(private) else shared


def existing_parent(path):
    return next(node for node in (path, *path.parents) if node.exists())


def volume_uuid(path):
    # APFS firmlinks give /Users and / the same st_dev on some macOS versions.
    # df resolves the actual writable Data volume even for a home-directory path.
    df = subprocess.check_output(['/bin/df', '-P', str(path)], timeout=10)
    device = df.decode().splitlines()[-1].split()[0]
    if not device.startswith('/dev/'):
        raise RuntimeError(f'请选择本地磁盘上的数据目录：{path}')
    disk = plistlib.loads(subprocess.check_output(
        ['/usr/sbin/diskutil', 'info', '-plist', device], timeout=10))
    identifier = disk.get('VolumeUUID')
    if not identifier:
        raise RuntimeError(f'无法识别数据卷：{path}')
    return identifier, Path(disk['MountPoint'])


def child_on_drive(root, path):
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or existing_parent(resolved).stat().st_dev != root.stat().st_dev:
        raise ValueError(f'项目路径离开数据目录或跨越磁盘：{path}')
    return resolved


def write_record(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as staging:
        pending = Path(staging) / 'record'
        pending.write_text(value)
        pending.chmod(0o600)
        os.replace(pending, path)


def check_drive(root=None, *, initialize=False):
    root = root if root is not None else DATA_ROOT
    if os.pathsep in str(root):
        raise ValueError('数据目录名称不能包含冒号，宿主工具需要将该目录加入 PATH。')
    existing = existing_parent(root)
    identity, mount = volume_uuid(existing)
    parts = root.parts
    if root == mount or root in (Path('/'), Path('/Volumes'), Path.home()):
        raise ValueError('请选择专用的项目子目录。')
    if len(parts) > 2 and parts[1] == 'Volumes' and mount != Path(*parts[:3]):
        raise RuntimeError(f'数据盘未挂载在原位置：{Path(*parts[:3])}')
    marker = root / MARKER
    if marker.is_symlink():
        raise RuntimeError(f'数据卷标记不是普通文件：{marker}')
    marked = marker.read_text().strip() if marker.is_file() else None
    selected = saved_location()
    remembered = selected is not None and selected[0] == root
    legacy = legacy_install(root)
    identifiers = ([marked] if marked is not None else []) + ([selected[1]] if remembered else [])
    if legacy and not identifiers:
        identifiers.append(LEGACY_VOLUME_UUID)
    if any(identifier != identity for identifier in identifiers):
        raise RuntimeError(f'该位置的磁盘与安装时不符，请重新挂载原数据盘：{root}')
    if not marked and not legacy and (remembered or not initialize):
        raise RuntimeError(f'未找到已登记的数据目录，请确认原目录仍可访问：{root}')
    if initialize:
        if not marked and not legacy and root.exists():
            conflicts = [item for item in root.iterdir() if item.name not in ('.DS_Store', 'cache', 'images')]
            if conflicts:
                raise ValueError(f'数据目录包含其他文件，请选择空文件夹或已安装的实例：{root}')
            for name in ('cache', 'images'):
                entry = root / name
                if entry.is_symlink() or (entry.exists() and not entry.is_dir()):
                    raise ValueError(f'预置资源目录不是普通文件夹：{entry}')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not marked:
            write_record(marker, identity + '\n')


def prepare_vm_disk(root):
    """Mark a new short-path instance before Lima creates its disk."""
    home = lima_home(root)
    if home == root / 'lima':
        return
    if (root / 'vm').is_symlink():
        raise RuntimeError('虚拟机数据目录不可为符号链接。')
    # A shared location is needed only for an unusually long or URL-unsafe home name.
    # Claim a private parent before placing the VM’s SSH keys and socket underneath it.
    home.parent.mkdir(mode=0o700, exist_ok=True)
    parent_stat = home.parent.lstat()
    if home.parent.is_symlink() or parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
        raise RuntimeError(f'虚拟机运行目录应由当前用户独占：{home.parent}')
    folder = child_on_drive(root, root / 'vm')
    folder.mkdir(mode=0o700, exist_ok=True)
    intent = folder / '.instance'
    if intent.is_symlink() or (intent.exists() and intent.read_text().strip() != str(home)):
        raise RuntimeError(f'虚拟机数据目录与当前安装位置不符：{folder}')
    if (folder / 'disk').exists() and not (home / 'wx/lima.yaml').is_file():
        raise RuntimeError(f'已登记的虚拟机配置丢失，请恢复对应实例：{home}')
    if not intent.exists():
        if any(folder.iterdir()):
            raise RuntimeError(f'虚拟机数据目录已有文件：{folder}')
        write_record(intent, str(home) + '\n')


def place_vm_disk(root):
    """Before first boot, keep the VZ guest disk (including Docker volumes) on the data drive."""
    home = lima_home(root)
    if home == root / 'lima':
        return
    instance = home / 'wx'
    if any(path.is_symlink() for path in (home.parent, home, instance, root / 'vm')):
        raise RuntimeError('虚拟机实例或数据目录不可为符号链接。')
    source = instance / 'disk'
    folder = child_on_drive(root, root / 'vm')
    target = folder / 'disk'
    intent = folder / '.instance'
    if not source.is_file():
        raise RuntimeError(f'虚拟机磁盘丢失，请检查原卷，无法启动：{source}')
    if not intent.exists() and not source.is_symlink():
        return  # An existing installation owns this disk; leave it in its original location.
    if intent.is_symlink() or not intent.is_file() or intent.read_text().strip() != str(home) or target.is_symlink():
        raise RuntimeError(f'虚拟机数据目录与当前实例不符：{folder}')
    if source.is_symlink():
        if source.readlink() != target or not target.is_file():
            raise RuntimeError(f'虚拟机磁盘链接与已登记的目标不符：{source}')
        return
    if not target.exists():
        try:
            os.link(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            with tempfile.TemporaryDirectory(dir=folder) as staging:
                pending = Path(staging) / 'disk'
                # macOS cp preserves sparse extents by default; never materialize the 55 GiB hole.
                subprocess.run(['/bin/cp', '-p', str(source), str(pending)], check=True)
                os.link(pending, target)
    elif not target.is_file() or not (source.samefile(target) or filecmp.cmp(source, target, shallow=False)):
        # A prior copy may have finished immediately before an interruption.
        raise RuntimeError(f'目标已有不同的虚拟机磁盘，已保留两份：{target}')
    with tempfile.TemporaryDirectory(dir=source.parent) as staging:
        link = Path(staging) / 'disk'
        link.symlink_to(target)
        os.replace(link, source)


def save_selection(root):
    """Commit the user's default only after all deployment probes succeed."""
    check_drive(root)
    identity = (root / MARKER).read_text().strip()
    if saved_location() != (root, identity):
        write_record(location_file(), f'{root}\n{identity}\n')


DATA_ROOT = resolve_root()
DOCKER_HOST = f'unix://{lima_home(DATA_ROOT)}/wx/sock/docker.sock'
