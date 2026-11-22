#!/usr/bin/env python3
"""Register and verify a locally recorded WeChat playback by its public share link."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
from wechat_preview import DATABASE, ROOT, SHARE_URL
from wechat_settings import (
    DEFAULT_STORAGE_PATH, SETTINGS_FILENAME, STORAGE_ROOT, indexed_path, read_settings, resolve_storage_path,
)


MEDIA = DEFAULT_STORAGE_PATH
SETTINGS_FILE = ROOT / "state" / "monitoring" / SETTINGS_FILENAME
SOURCE = "wechat-playback-recording"


def media_path(value):
    return indexed_path(STORAGE_ROOT, MEDIA, str(value))


def configured_destination(relative):
    """Store the selected path once before a new recording starts."""
    selected = read_settings(SETTINGS_FILE, default_storage_path=MEDIA)['storage_path']
    folder = resolve_storage_path(selected, STORAGE_ROOT, STORAGE_ROOT)
    if folder == MEDIA.resolve():
        return relative.as_posix()
    return './' + (folder.relative_to(STORAGE_ROOT.resolve()) / relative).as_posix()


def verify_streams(path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_name,codec_type,width,height,channels:stream_disposition=attached_pic",
         "-of", "json", str(path)], check=True, capture_output=True, timeout=45,
    )
    media = json.loads(probe.stdout)
    streams = media.get("streams", [])
    video = any(s.get("codec_type") == "video" and s.get("codec_name") not in (None, "unknown")
                and s.get("width", 0) > 0 and s.get("height", 0) > 0
                and not s.get("disposition", {}).get("attached_pic") for s in streams)
    audio = any(s.get("codec_type") == "audio" and s.get("codec_name") not in (None, "unknown")
                and s.get("channels", 0) > 0 for s in streams)
    duration = float(media.get("format", {}).get("duration", 0))
    if not (video and audio and math.isfinite(duration) and duration > 0):
        raise ValueError("recording requires a video stream, an audio stream and a positive duration")


def file_signature(path, probe=False):
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise ValueError("expected a nonempty regular file")
    if probe:
        verify_streams(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(source.fileno())
    for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
        if getattr(before, key) != getattr(after, key):
            raise OSError("recording changed during verification")
    if not os.path.samestat(after, path.stat()):
        raise OSError("recording was replaced during verification")
    return after.st_size, digest.hexdigest()


def work_key(db, url):
    row = db.execute("""
        SELECT l.fingerprint, s.cover, r.reason FROM links l
        LEFT JOIN signatures s USING(fingerprint) LEFT JOIN review r ON r.url=l.url
        WHERE l.url=?
    """, (url,)).fetchone()
    if row is None:
        raise ValueError("unknown share link; index its public preview first")
    if not row[1] or row[2]:
        raise ValueError("share link needs review or its cover has not been verified")
    return row[0]


def entry(db, key):
    row = db.execute("SELECT path, size, sha256, source FROM recordings WHERE work_key=?",
                     (key,)).fetchone()
    return dict(row) if row else None


def register(db, url, value, replace):
    work_key(db, url)
    value = str(value)
    root = STORAGE_ROOT.resolve()
    legacy = MEDIA.resolve()
    if Path(value).is_absolute():
        path = Path(value).resolve()
        if path.is_relative_to(legacy):
            value = path.relative_to(legacy).as_posix()
        else:
            value = './' + path.relative_to(root).as_posix()
    path = media_path(value)
    size, digest = file_signature(path, probe=True)
    legacy_path = path.relative_to(legacy).as_posix() if path.is_relative_to(legacy) else None
    root_path = './' + path.relative_to(root).as_posix()
    index_path, alias = (root_path, legacy_path) if value.startswith('./') else (legacy_path, root_path)
    item = dict(path=index_path, size=size, sha256=digest, source=SOURCE)
    with db:
        db.execute("BEGIN IMMEDIATE")
        key = work_key(db, url)
        db.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                work_key TEXT PRIMARY KEY REFERENCES works(fingerprint), path TEXT NOT NULL UNIQUE,
                size INTEGER NOT NULL CHECK(size>0), sha256 TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source='wechat-playback-recording')
            )
        """)
        old = entry(db, key)
        if old and old['path'] == alias:
            item['path'] = alias
        if old == item:
            return dict(status="already_registered", work_key=key, **old)
        if old and not replace:
            raise ValueError("another path or file digest is registered; review it before using --replace")
        if alias and db.execute("SELECT 1 FROM recordings WHERE path=? AND work_key<>?", (alias, key)).fetchone():
            raise ValueError("recording path is already assigned to another work")
        db.execute("""
            INSERT INTO recordings(work_key,path,size,sha256,source) VALUES (:work_key,:path,:size,:sha256,:source)
            ON CONFLICT(work_key) DO UPDATE SET path=excluded.path, size=excluded.size,
                sha256=excluded.sha256, source=excluded.source
        """, dict(work_key=key, **item))
    return dict(status="replaced" if old else "registered", work_key=key, **item)


def lookup(db, url):
    key = work_key(db, url)
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recordings'").fetchone()
    item = entry(db, key) if exists else None
    if item is None:
        return dict(status="not_registered", work_key=key)
    try:
        path = media_path(item["path"])
        if path.stat().st_size != item["size"]:
            status = "changed"
        else:
            status = "verified" if file_signature(path)[1] == item["sha256"] else "changed"
    except FileNotFoundError:
        status = "missing"
    except (OSError, ValueError) as exc:
        return dict(status="unverifiable", work_key=key, reason=str(exc), **item)
    return dict(status=status, work_key=key, **item)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("lookup").add_argument("url")
    add = sub.add_parser("register")
    add.add_argument("url")
    add.add_argument("file", type=Path,
                     help="recording path relative to media/微信视频号, or an absolute path under media")
    add.add_argument("--source", choices=[SOURCE], required=True,
                     help="confirm this file was recorded from playback in WeChat")
    add.add_argument("--replace", action="store_true", help="replace a previously registered file after review")
    args = parser.parse_args()
    if not SHARE_URL.fullmatch(args.url):
        parser.error("expected a public https://weixin.qq.com/sph/<share-token> URL")
    mode = "ro" if args.action == "lookup" else "rw"
    try:
        with sqlite3.connect(DATABASE.as_uri() + "?mode=" + mode, uri=True, timeout=15) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            result = lookup(db, args.url) if mode == "ro" else register(db, args.url, args.file, args.replace)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"verified", "registered", "already_registered", "replaced"} else 1
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(status="error", reason=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
