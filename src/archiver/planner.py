from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .db import transaction
from .hashing import hash_file

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int, int], None]
RAW_EXTENSIONS = {".raw", ".cr2", ".nef", ".arw"}


def _archive_kind_for(path_value: str, category: str) -> str:
    ext = Path(path_value).suffix.lower()
    if category == "document":
        return "doc"
    if category == "video":
        return "movie"
    if category == "photo":
        if ext in RAW_EXTENSIONS:
            return "raw"
        return "pic"
    return "other"


def _should_report_progress(index: int, total: int, last_report_at: float, now: float) -> bool:
    if index == 1 or index == total:
        return True
    if index % 100 == 0:
        return True
    if now - last_report_at >= 2.0:
        return True
    return False


@dataclass(slots=True)
class PlanResult:
    disc_code: str | None
    file_count: int
    total_bytes: int


def _next_disc_code(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*) AS count FROM discs").fetchone()
    return f"DISC-{row['count'] + 1:04d}"


def _disc_label(date_from: str, date_to: str) -> str:
    return f"archive {date_from[:7]}..{date_to[:7]}"


def _disc_relative_path(category: str, media_date: str, relative_path: str) -> str:
    year = media_date[:4]
    month = media_date[5:7]
    if category == "photo":
        prefix = "photos"
    elif category == "video":
        prefix = "videos"
    elif category == "document":
        prefix = "doc"
    else:
        prefix = "other"
    bucket = f"{prefix}/{year}/{month}"
    return f"{bucket}/{Path(relative_path).name}"


def _write_disc_indexes(
    conn: sqlite3.Connection,
    settings: Settings,
    disc_id: int,
    disc_code: str,
    label: str,
    date_from: str,
    date_to: str,
) -> None:
    settings.manifests_dir.mkdir(parents=True, exist_ok=True)
    csv_path = settings.manifests_dir / f"{disc_code}.csv"
    json_path = settings.manifests_dir / f"{disc_code}.json"
    rows = conn.execute(
        """
        SELECT
            f.source_root,
            f.relative_path,
            f.absolute_path,
            f.category,
            f.media_date,
            df.relative_path_on_disc,
            df.content_hash,
            df.size_bytes
        FROM disc_files df
        JOIN files f ON f.id = df.file_id
        WHERE df.disc_id = ?
        ORDER BY f.media_date ASC, f.relative_path ASC
        """,
        (disc_id,),
    ).fetchall()

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "disc_code",
                "label",
                "source_root",
                "relative_path",
                "absolute_path",
                "category",
                "media_date",
                "relative_path_on_disc",
                "size_bytes",
                "content_hash",
                "source_folder",
                "archive_kind",
            ]
        )
        for row in rows:
            archive_kind = _archive_kind_for(row["relative_path"], row["category"])
            writer.writerow(
                [
                    disc_code,
                    label,
                    row["source_root"],
                    row["relative_path"],
                    row["absolute_path"],
                    row["category"],
                    row["media_date"],
                    row["relative_path_on_disc"],
                    row["size_bytes"],
                    row["content_hash"],
                    Path(row["source_root"]).name,
                    archive_kind,
                ]
            )

    payload = {
        "disc_code": disc_code,
        "label": label,
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": datetime.now(UTC).isoformat(),
        "csv_index": f"{disc_code}.csv",
        "files": [
            {
                "source_root": row["source_root"],
                "relative_path": row["relative_path"],
                "absolute_path": row["absolute_path"],
                "category": row["category"],
                "media_date": row["media_date"],
                "relative_path_on_disc": row["relative_path_on_disc"],
                "size_bytes": row["size_bytes"],
                "content_hash": row["content_hash"],
                "archive_kind": _archive_kind_for(row["relative_path"], row["category"]),
            }
            for row in rows
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def plan_disc(
    conn: sqlite3.Connection,
    settings: Settings,
    progress_callback: ProgressCallback | None = None,
) -> PlanResult:
    logger.info("planning disc with limit=%d bytes", settings.planning_limit_bytes)
    already_planned = conn.execute(
        "SELECT disc_code, file_count, planned_bytes FROM discs WHERE status IN ('planned', 'approved', 'staged', 'burning', 'burned') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if already_planned is not None:
        logger.info("existing active disc found: %s", already_planned["disc_code"])
        return PlanResult(
            disc_code=already_planned["disc_code"],
            file_count=already_planned["file_count"],
            total_bytes=already_planned["planned_bytes"],
        )

    rows = conn.execute(
        """
        SELECT id, absolute_path, relative_path, size_bytes, category, media_date
        FROM files
        WHERE status IN ('new', 'changed_after_archive')
        ORDER BY media_date ASC, relative_path ASC
        """
    ).fetchall()

    picked: list[sqlite3.Row] = []
    total_bytes = 0
    for row in rows:
        if total_bytes + row["size_bytes"] > settings.planning_limit_bytes and picked:
            break
        picked.append(row)
        total_bytes += row["size_bytes"]

    if not picked:
        logger.info("no files available for planning")
        return PlanResult(disc_code=None, file_count=0, total_bytes=0)

    disc_code = _next_disc_code(conn)
    date_from = picked[0]["media_date"]
    date_to = picked[-1]["media_date"]
    now = datetime.now(UTC).isoformat()
    label = _disc_label(date_from, date_to)

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO discs (disc_code, label, status, planned_bytes, file_count, date_from, date_to, created_at, updated_at)
            VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?)
            """,
            (disc_code, label, total_bytes, len(picked), date_from, date_to, now, now),
        )
        disc_id = cursor.lastrowid
        last_report_at = 0.0
        total_files = len(picked)
        for index, row in enumerate(picked, start=1):
            content_hash = hash_file(Path(row["absolute_path"]))
            relative_path_on_disc = _disc_relative_path(row["category"], row["media_date"], row["relative_path"])
            conn.execute(
                """
                INSERT INTO disc_files (disc_id, file_id, relative_path_on_disc, content_hash, size_bytes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (disc_id, row["id"], relative_path_on_disc, content_hash, row["size_bytes"]),
            )
            conn.execute(
                """
                UPDATE files
                SET content_hash = ?, status = 'planned', disc_id = ?
                WHERE id = ?
                """,
                (content_hash, disc_id, row["id"]),
            )
            progress_now = time.monotonic()
            if progress_callback is not None:
                progress_callback(disc_code, index, total_files)
            if _should_report_progress(index, total_files, last_report_at, progress_now):
                logger.info(
                    "hash progress for %s: %d/%d files (%.1f%%)",
                    disc_code,
                    index,
                    total_files,
                    (index / total_files) * 100,
                )
                last_report_at = progress_now
        _write_disc_indexes(conn, settings, disc_id, disc_code, label, date_from, date_to)

    logger.info(
        "planned disc %s with %d files and %d bytes, range %s..%s",
        disc_code,
        len(picked),
        total_bytes,
        date_from,
        date_to,
    )
    return PlanResult(disc_code=disc_code, file_count=len(picked), total_bytes=total_bytes)


def approve_disc(conn: sqlite3.Connection, disc_code: str) -> bool:
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        row = conn.execute("SELECT id FROM discs WHERE disc_code = ?", (disc_code,)).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE discs SET status = 'approved', approved_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        conn.execute(
            "UPDATE files SET status = 'approved' WHERE disc_id = ? AND status = 'planned'",
            (row["id"],),
        )
    logger.info("approved disc %s", disc_code)
    return True
