from __future__ import annotations

import sqlite3


def pending_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM files WHERE status IN ('new', 'changed_after_archive')"
    ).fetchone()
    return int(row["total"])


def active_disc(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT disc_code, label, status, planned_bytes, file_count, date_from, date_to, approved_at
        FROM discs
        WHERE status IN ('planned', 'approved', 'staged', 'burning', 'burned')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def status_summary(conn: sqlite3.Connection) -> dict[str, object]:
    counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM files GROUP BY status ORDER BY status"
        ).fetchall()
    }
    pending_bytes_total = pending_bytes(conn)
    planned_disc = active_disc(conn)
    recent_discs = conn.execute(
        """
        SELECT disc_code, label, status, planned_bytes, file_count, date_from, date_to, created_at
        FROM discs
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()
    pending_files = conn.execute(
        """
        SELECT relative_path, size_bytes, category, media_date, status
        FROM files
        WHERE status IN ('new', 'changed_after_archive', 'planned', 'approved', 'staged', 'burning', 'burned')
        ORDER BY media_date ASC, relative_path ASC
        LIMIT 200
        """
    ).fetchall()
    roots = conn.execute(
        """
        SELECT source_root, MAX(last_seen_at) AS last_seen_at
        FROM files
        GROUP BY source_root
        ORDER BY source_root
        """
    ).fetchall()
    return {
        "counts": counts,
        "pending_bytes": pending_bytes_total,
        "planned_disc": planned_disc,
        "recent_discs": recent_discs,
        "pending_files": pending_files,
        "roots": roots,
    }
