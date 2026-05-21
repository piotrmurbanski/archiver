from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    source_root TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    content_hash TEXT,
    category TEXT NOT NULL,
    media_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    disc_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    archived_at TEXT,
    changed_after_archive INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_root, relative_path)
);

CREATE TABLE IF NOT EXISTS discs (
    id INTEGER PRIMARY KEY,
    disc_code TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    planned_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    date_from TEXT,
    date_to TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disc_files (
    disc_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    relative_path_on_disc TEXT NOT NULL,
    content_hash TEXT,
    size_bytes INTEGER NOT NULL,
    PRIMARY KEY (disc_id, file_id),
    FOREIGN KEY(disc_id) REFERENCES discs(id),
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_disc_id ON files(disc_id);
CREATE INDEX IF NOT EXISTS idx_discs_status ON discs(status);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
