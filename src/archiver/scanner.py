from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .db import transaction

logger = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".raw", ".cr2", ".nef", ".arw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".3gp"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".odt", ".xls", ".xlsx"}
DATE_PATTERN = re.compile(r"(20\d{2})[-_/]?(0[1-9]|1[0-2])")


@dataclass(slots=True)
class ScanStats:
    scanned_files: int = 0
    new_files: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    missing_roots: int = 0
    skipped_files: int = 0
    offline_roots: int = 0


def root_is_available(root: Path) -> bool:
    try:
        if not root.exists() or not root.is_dir():
            return False
        next(root.iterdir(), None)
        return True
    except OSError:
        return False


def classify_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    return "other"


def media_date_for(path: Path, stat_result) -> str:
    match = DATE_PATTERN.search(path.as_posix())
    if match:
        return f"{match.group(1)}-{match.group(2)}-01T00:00:00+00:00"
    return datetime.fromtimestamp(stat_result.st_mtime, UTC).isoformat()


def _fingerprint(size_bytes: int, mtime_ns: int) -> str:
    return f"{size_bytes}:{mtime_ns}"


def scan_sources(conn: sqlite3.Connection, settings: Settings) -> ScanStats:
    stats = ScanStats()
    now = datetime.now(UTC).isoformat()
    logger.info("scan started for %d roots", len(settings.roots))
    with transaction(conn):
        for root in settings.roots:
            if not root.exists():
                stats.missing_roots += 1
                logger.warning("scan skipped missing root: %s", root)
                continue
            if not root_is_available(root):
                stats.offline_roots += 1
                logger.warning("scan skipped offline root: %s", root)
                continue
            logger.info("scanning root: %s", root)
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat_result = path.stat()
                except OSError:
                    stats.skipped_files += 1
                    logger.warning("could not stat file: %s", path)
                    continue
                stats.scanned_files += 1
                relative_path = path.relative_to(root).as_posix()
                fingerprint = _fingerprint(stat_result.st_size, stat_result.st_mtime_ns)
                category = classify_file(path)
                media_date = media_date_for(path, stat_result)
                existing = conn.execute(
                    """
                    SELECT id, fingerprint, status
                    FROM files
                    WHERE source_root = ? AND relative_path = ?
                    """,
                    (str(root), relative_path),
                ).fetchone()

                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO files (
                            source_root, relative_path, absolute_path, size_bytes, mtime_ns,
                            fingerprint, category, media_date, status, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                        """,
                        (
                            str(root),
                            relative_path,
                            str(path),
                            stat_result.st_size,
                            stat_result.st_mtime_ns,
                            fingerprint,
                            category,
                            media_date,
                            now,
                            now,
                        ),
                    )
                    stats.new_files += 1
                    continue

                if existing["fingerprint"] == fingerprint:
                    conn.execute(
                        "UPDATE files SET absolute_path = ?, last_seen_at = ? WHERE id = ?",
                        (str(path), now, existing["id"]),
                    )
                    stats.unchanged_files += 1
                    continue

                next_status = "changed_after_archive" if existing["status"] == "verified" else "new"
                conn.execute(
                    """
                    UPDATE files
                    SET absolute_path = ?, size_bytes = ?, mtime_ns = ?, fingerprint = ?,
                        category = ?, media_date = ?, status = ?, disc_id = NULL,
                        archived_at = NULL, changed_after_archive = 1, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(path),
                        stat_result.st_size,
                        stat_result.st_mtime_ns,
                        fingerprint,
                        category,
                        media_date,
                        next_status,
                        now,
                        existing["id"],
                    ),
                )
                stats.changed_files += 1
    logger.info(
        "scan completed: scanned=%d new=%d changed=%d unchanged=%d skipped=%d missing_roots=%d offline_roots=%d",
        stats.scanned_files,
        stats.new_files,
        stats.changed_files,
        stats.unchanged_files,
        stats.skipped_files,
        stats.missing_roots,
        stats.offline_roots,
    )
    return stats
