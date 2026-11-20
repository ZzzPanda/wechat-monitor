#!/usr/bin/env python3
"""Read the visible metadata of public WeChat Channels share pages into SQLite."""

import argparse
from datetime import date
import hashlib
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


from wechat_paths import DATA_ROOT as ROOT, check_drive

DATABASE = ROOT / "state" / "wechat-pilot" / "previews.sqlite3"
SHARE_URL = re.compile(r"https://weixin\.qq\.com/sph/[A-Za-z0-9_-]+")
FIELD_CLASSES = {"author-name": "author", "feed-desc-wrap": "caption",
                 "feed-create-time-wrap": "published"}
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
COVER_LIMIT = 2 * 1024 * 1024


class VisiblePreview(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.stack = []
        self.covers = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "img" and "video-player" in classes and attrs.get("src"):
            self.covers.add(attrs["src"])
        field = next((FIELD_CLASSES[c] for c in classes if c in FIELD_CLASSES), None)
        if field:
            if field in self.values:
                raise ValueError(f"ambiguous preview field: {field}")
            self.values[field] = []
        elif self.stack:
            field = self.stack[-1][1]
        if tag not in VOID:
            self.stack.append((tag, field))
        elif tag == "br" and field:
            self.values[field].append("\n")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            self.values[self.stack[-1][1]].append(data)

    def result(self):
        fields = {k: "".join(v).strip() for k, v in self.values.items()}
        if any(not fields.get(k) for k in FIELD_CLASSES.values()):
            raise ValueError("public preview is incomplete; no database update")
        match = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", fields["published"])
        if not match:
            raise ValueError("unrecognized public date")
        fields["published"] = date(*map(int, match.groups())).isoformat()
        return fields


def validate_cover_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() != "finder.video.qq.com":
        raise ValueError("preview cover origin unrecognized; manual review required")


class CoverRedirect(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_cover_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def cover_hash(url):
    validate_cover_url(url)
    opener = build_opener(CoverRedirect())
    for attempt in range(2):
        try:
            with opener.open(url, timeout=8) as response:
                mime = response.headers.get_content_type()
                length = response.headers.get("Content-Length")
                if response.status != 200 or (length and int(length) > COVER_LIMIT):
                    raise ValueError("unexpected cover response or size")
                data = response.read(COVER_LIMIT + 1)
                if length and len(data) != int(length):
                    raise OSError("incomplete cover")
                if not 0 < len(data) <= COVER_LIMIT:
                    raise ValueError("empty or oversized cover")
                if mime in ("image/jpg", "image/jpeg"):
                    valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
                elif mime == "image/png":
                    valid = data.startswith(b"\x89PNG\r\n\x1a\n") and data[-8:-4] == b"IEND"
                elif mime == "image/webp":
                    valid = data[:4] == b"RIFF" and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data)
                else:
                    valid = False
                if not valid:
                    raise ValueError("unsupported cover format or invalid image bytes")
                return hashlib.sha256(data).hexdigest()
        except HTTPError as exc:
            if attempt or (exc.code < 500 and exc.code != 429):
                raise OSError(f"cover HTTP {exc.code}") from exc
        except (URLError, OSError, http.client.HTTPException) as exc:
            if attempt:
                raise OSError("cover could not be read completely after two attempts") from exc
        time.sleep(0.5)


def read_preview(chrome, url):
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="wechat-preview-") as profile:
        result = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--disable-extensions",
             "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
             "--user-data-dir=" + profile, "--virtual-time-budget=15000", "--dump-dom", url],
            capture_output=True, check=True, timeout=45,
        )
    parser = VisiblePreview()
    parser.feed(result.stdout.decode("utf-8"))
    parser.close()
    fields = parser.result()
    if len(parser.covers) != 1:
        raise ValueError("missing or ambiguous cover; no database update")
    fields["cover_sha256"] = cover_hash(parser.covers.pop())
    return fields


def record(db, url, fields):
    cover = fields.get("cover_sha256", "")
    if not isinstance(cover, str) or not re.fullmatch(r"[0-9a-f]{64}", cover) or cover == hashlib.sha256(b"").hexdigest():
        raise ValueError("verified cover digest required; no database update")
    parts = [unicodedata.normalize("NFC", " ".join(fields[k].split()))
             for k in ("author", "published", "caption")]
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    metadata = hashlib.sha256(payload).hexdigest()
    fingerprint = hashlib.sha256(bytes.fromhex(metadata + cover)).hexdigest()
    result = dict(status="ok", added=0, local_fingerprint=fingerprint, **fields)
    with db:
        db.execute("BEGIN IMMEDIATE")
        known = db.execute("""
            SELECT links.fingerprint, signatures.metadata, signatures.cover
            FROM links LEFT JOIN signatures USING(fingerprint) WHERE url = ?
        """, (url,)).fetchone()
        if known:
            row_key, previous_metadata, previous_cover = known
            if (previous_metadata or row_key) != metadata or (previous_cover and previous_cover != cover):
                reason = "known share link changed metadata or cover"
                db.execute("""
                    INSERT INTO review(url, metadata, cover, reason) VALUES (?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET metadata=excluded.metadata,
                        cover=excluded.cover, reason=excluded.reason, observed_at=CURRENT_TIMESTAMP
                """, (url, metadata, cover, reason))
                return dict(result, status="needs_review", reason=reason)
            if previous_cover is None:
                # Keep existing works and links intact while adding a verified cover.
                db.execute("INSERT INTO signatures VALUES (?, ?, ?)", (row_key, metadata, cover))
            if db.execute("SELECT 1 FROM review WHERE url=?", (url,)).fetchone():
                return dict(result, status="needs_review", reason="prior change still awaits review")
            return result
        match = db.execute("SELECT fingerprint FROM signatures WHERE metadata=? AND cover=?",
                           (metadata, cover)).fetchone()
        if match:
            row_key = match[0]
        else:
            legacy = db.execute("""
                SELECT 1 FROM works LEFT JOIN signatures USING(fingerprint)
                WHERE works.fingerprint=? AND signatures.cover IS NULL
            """, (metadata,)).fetchone()
            if legacy:
                return dict(result, status="needs_review", reason="revisit an original link to verify its cover first")
            row_key = fingerprint
            db.execute("INSERT INTO works(fingerprint, author, published, caption) VALUES (?, ?, ?, ?)",
                       (row_key, fields["author"], fields["published"], fields["caption"]))
            db.execute("INSERT INTO signatures VALUES (?, ?, ?)", (row_key, metadata, cover))
            result["added"] = 1
        db.execute("INSERT INTO links(url, fingerprint) VALUES (?, ?)", (url, row_key))
    return result


def prepare(db):
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS works (
            fingerprint TEXT PRIMARY KEY, author TEXT NOT NULL, published TEXT NOT NULL,
            caption TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS links (
            url TEXT PRIMARY KEY, fingerprint TEXT NOT NULL REFERENCES works(fingerprint)
        );
        CREATE TABLE IF NOT EXISTS signatures (
            fingerprint TEXT PRIMARY KEY REFERENCES works(fingerprint), metadata TEXT NOT NULL,
            cover TEXT NOT NULL, UNIQUE(metadata, cover)
        );
        CREATE TABLE IF NOT EXISTS review (
            url TEXT PRIMARY KEY REFERENCES links(url), metadata TEXT NOT NULL, cover TEXT NOT NULL,
            reason TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="+", metavar="WECHAT_SHARE_URL")
    args = ap.parse_args()
    for url in args.urls:
        if not SHARE_URL.fullmatch(url):
            ap.error("expected https://weixin.qq.com/sph/<share-token> without extra parameters")
    chrome = next((p for p in (shutil.which("google-chrome"), shutil.which("chromium"),
                               "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                   if p and os.access(p, os.X_OK)), None)
    if not chrome:
        ap.error("Chrome or Chromium is required")
    check_drive()
    (ROOT / "tmp").mkdir(exist_ok=True)
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    failed = False
    with sqlite3.connect(DATABASE, timeout=15) as db:
        prepare(db)
        for index, url in enumerate(args.urls, 1):
            try:
                result = record(db, url, read_preview(chrome, url))
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if result["status"] != "ok":
                    failed = True
            except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
                failed = True
                print(f"Input {index}: {exc}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
