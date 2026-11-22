"""Timestamped events shared by the dashboard and Mac workers."""
from contextlib import closing
import sqlite3
import time


CATEGORIES = ('system', 'crawl', 'search')
LEVELS = ('info', 'warning', 'error')
FILENAME = 'events.sqlite3'
RESULT_LIMIT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time INTEGER NOT NULL, category TEXT NOT NULL,
    level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_time ON events(time);
CREATE INDEX IF NOT EXISTS events_by_category ON events(category, time);
"""


def write_event(directory, category, level, message, *, when=None):
    """Record an event without blocking the underlying worker on a log failure."""
    if category not in CATEGORIES or level not in LEVELS:
        return
    stamp = int(time.time()) if when is None else when
    try:
        if not directory.is_dir():
            return
        with closing(sqlite3.connect(directory / FILENAME, timeout=0.5)) as db, db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO events(time, category, level, message) VALUES (?, ?, ?, ?)',
                       (stamp, category, level, ' '.join(message.split())[:500]))
    except (OSError, sqlite3.Error):
        # The capture and search workers have their own primary result channels.
        pass


def read_events(directory, category, start, end, limit):
    path = directory / FILENAME
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
            return []
        db.row_factory = sqlite3.Row
        query = 'SELECT time, category, level, message FROM events WHERE time BETWEEN ? AND ?'
        args = (start, end)
        if category != 'all':
            query += ' AND category=?'
            args += (category,)
        return [dict(row) for row in db.execute(query + ' ORDER BY time DESC, id DESC LIMIT ?', (*args, limit))]
