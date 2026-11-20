#!/usr/bin/env python3
"""Cache a representative local video frame and duration for the dashboard."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_paths import DATA_ROOT, check_drive


def create_poster(record, media, cache):
    key, relative, digest, size = record
    if not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('invalid recording ID')
    video = (media / relative).resolve(strict=True)
    if not video.is_relative_to(media) or not video.is_file():
        raise ValueError('invalid recording path')
    stat = video.stat()
    if stat.st_size != size:
        raise ValueError('recording size differs from the index')
    image = cache / (key + '.jpg')
    sidecar = cache / (key + '.json')
    if image.is_file() and sidecar.is_file():
        try:
            old = json.loads(sidecar.read_text())
            if old.get('sha256') == digest and old.get('mtime_ns') == stat.st_mtime_ns:
                return False
        except (ValueError, AttributeError):
            pass
    duration = float(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(video)],
        text=True, timeout=15))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('invalid video duration')
    # Step past the opening controls; choose a representative frame from a short interval.
    start = min(duration * .12, 18)
    with tempfile.TemporaryDirectory(dir=cache) as scratch:
        jpg = Path(scratch) / 'poster.jpg'
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                        '-ss', f'{start:.3f}', '-i', str(video), '-map', '0:v:0',
                        '-vf', 'scale=640:420:force_original_aspect_ratio=decrease,thumbnail=16',
                        '-frames:v', '1', '-q:v', '3', str(jpg)], check=True, timeout=35)
        if not jpg.is_file() or not jpg.stat().st_size:
            raise ValueError('could not select a frame')
        metadata = Path(scratch) / 'duration.json'
        metadata.write_text(json.dumps(dict(duration=round(duration, 3), sha256=digest,
                                            mtime_ns=stat.st_mtime_ns)) + '\n')
        os.replace(jpg, image)
        os.replace(metadata, sidecar)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', help='only update this recording')
    args = parser.parse_args()
    if args.key and not re.fullmatch(r'[0-9a-f]{64}', args.key):
        parser.error('expected a 64-character recording ID')
    check_drive()
    media = (DATA_ROOT / 'media/微信视频号').resolve()
    cache = (DATA_ROOT / 'state/posters').resolve()
    if not media.is_relative_to(DATA_ROOT) or not cache.is_relative_to(DATA_ROOT):
        raise ValueError('preview storage is outside the project')
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = DATA_ROOT / 'state/wechat-pilot/previews.sqlite3'
    if not database.is_file():
        return 0
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recordings'").fetchone():
            return 0
        sql = 'SELECT work_key, path, sha256, size FROM recordings'
        rows = db.execute(sql + ' WHERE work_key=?', (args.key,)) if args.key else db.execute(sql)
        entries = rows.fetchall()
    made = errors = 0
    for row in entries:
        try:
            made += create_poster(row, media, cache)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors += 1
            print(f'无法生成作品 {row[0]} 的预览：{exc}', file=sys.stderr)
    if not args.key:
        print(f'历史作品 {len(entries)} 条，新生成封面 {made} 张。')
    return int(errors > 0)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
