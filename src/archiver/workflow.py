from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .notifier import send_notification
from .planner import plan_disc
from .repository import active_disc, pending_bytes
from .scanner import ScanProgressCallback, root_is_available, scan_sources

logger = logging.getLogger(__name__)
PlanProgressCallback = Callable[[str, int, int], None]


@dataclass(slots=True)
class ScanCycleResult:
    skipped: bool
    message: str


def _format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def run_scan_cycle(
    conn: sqlite3.Connection,
    settings: Settings,
    plan_progress_callback: PlanProgressCallback | None = None,
    scan_progress_callback: ScanProgressCallback | None = None,
) -> ScanCycleResult:
    unavailable_roots = [str(root) for root in settings.roots if not root_is_available(root)]
    if unavailable_roots:
        message = "scan skipped: NAS root unavailable\n" + "\n".join(f"  offline: {root}" for root in unavailable_roots)
        logger.warning(message)
        return ScanCycleResult(skipped=True, message=message)

    stats = scan_sources(conn, settings, progress_callback=scan_progress_callback)
    summary = (
        "scan completed: "
        f"scanned={stats.scanned_files} "
        f"new={stats.new_files} "
        f"changed={stats.changed_files} "
        f"unchanged={stats.unchanged_files} "
        f"missing_roots={stats.missing_roots} "
        f"skipped={stats.skipped_files} "
        f"offline_roots={stats.offline_roots}"
    )

    pending_total = pending_bytes(conn)
    current_disc = active_disc(conn)
    if settings.auto_plan and current_disc is None and pending_total >= settings.planning_limit_bytes:
        result = plan_disc(conn, settings, progress_callback=plan_progress_callback)
        if result.disc_code is not None:
            message = (
                f"{result.disc_code}: {result.file_count} files, "
                f"{_format_bytes(result.total_bytes)} ready for approval"
            )
            send_notification(settings, "Archiver: disc ready", message)
            logger.info("auto-planned: %s", message)
            return ScanCycleResult(skipped=False, message=f"{summary}\nauto-planned: {message}")

    return ScanCycleResult(skipped=False, message=summary)
