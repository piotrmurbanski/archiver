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
        WHERE status IN ('planned', 'approved', 'staged', 'burning', 'burned', 'verify_failed', 'burn_failed')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def status_summary(conn: sqlite3.Connection) -> dict[str, object]:
    total_files_row = conn.execute("SELECT COUNT(*) AS count FROM files").fetchone()
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
    roots = conn.execute(
        """
        SELECT source_root, MAX(last_seen_at) AS last_seen_at
        FROM files
        GROUP BY source_root
        ORDER BY source_root
        """
    ).fetchall()
    return {
        "total_files": int(total_files_row["count"]),
        "counts": counts,
        "pending_bytes": pending_bytes_total,
        "planned_disc": planned_disc,
        "recent_discs": recent_discs,
        "roots": roots,
    }
