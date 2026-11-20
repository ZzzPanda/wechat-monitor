"""Persistent runtime locations, independent of the Git checkout."""
from pathlib import Path
import plistlib
import subprocess

VOLUME = Path('/Volumes/ssd1')
DISK_UUID = '854330B0-E6A8-4BB0-8D7D-767BBEA0AF36'
DATA_ROOT = VOLUME / 'wechat-monitor'
DOCKER_HOST = f'unix://{DATA_ROOT}/lima/wx/sock/docker.sock'


def check_drive():
    info = plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', str(VOLUME)], timeout=8))
    if (info.get('VolumeUUID') != DISK_UUID or info.get('MountPoint') != str(VOLUME)
            or DATA_ROOT.stat().st_dev != VOLUME.stat().st_dev):
        raise RuntimeError('请挂载指定的 1 TB 外置盘并运行 ./deploy.sh。')
