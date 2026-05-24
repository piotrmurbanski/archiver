from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from .burner import burn_disc, speed_verify_disc_from_device, stage_disc, verify_disc, verify_disc_from_device
from .config import load_settings
from .db import connect, init_db
from .logging_setup import configure_logging
from .notifier import send_notification
from .planner import approve_disc, plan_disc
from .repository import status_summary
from .status_store import load_status_payload, save_status_payload
from .workflow import run_scan_cycle
from .web import create_app

logger = logging.getLogger(__name__)


def _format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def _backup_db(source_conn: sqlite3.Connection, source_path: Path, backup_path: Path) -> Path:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(backup_path)
    try:
        source_conn.backup(destination)
    finally:
        destination.close()
    logger.info("database backup created: source=%s backup=%s", source_path, backup_path)
    return backup_path


def _prune_old_backups(backups_dir: Path, keep: int) -> None:
    if keep < 1 or not backups_dir.exists():
        return
    backups = sorted(
        backups_dir.glob("archive-*.db"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale_backup in backups[keep:]:
        stale_backup.unlink(missing_ok=True)
        logger.info("removed old database backup: %s", stale_backup)


def _auto_backup_after_verify(conn: sqlite3.Connection, settings, disc_code: str) -> Path | None:
    if not settings.auto_backup_after_verify:
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = settings.backups_dir / f"archive-{disc_code.lower()}-{timestamp}.db"
    created = _backup_db(conn, settings.db_path, backup_path)
    _prune_old_backups(settings.backups_dir, settings.backup_keep)
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archiver")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")
    backup = subparsers.add_parser("backup-db")
    backup.add_argument("--output", default=None)
    subparsers.add_parser("scan")
    subparsers.add_parser("start")
    subparsers.add_parser("status")
    subparsers.add_parser("plan")
    stage = subparsers.add_parser("stage")
    stage.add_argument("disc_code")
    burn = subparsers.add_parser("burn")
    burn.add_argument("disc_code")
    burn_worker = subparsers.add_parser("burn-worker")
    burn_worker.add_argument("disc_code")
    verify_worker = subparsers.add_parser("verify-worker")
    verify_worker.add_argument("disc_code")
    speed_verify_worker = subparsers.add_parser("speed-verify-worker")
    speed_verify_worker.add_argument("disc_code")
    verify = subparsers.add_parser("verify")
    verify.add_argument("disc_code")
    verify.add_argument("--mount-path", default=None)
    speed_verify = subparsers.add_parser("speed-verify")
    speed_verify.add_argument("disc_code")

    approve = subparsers.add_parser("approve")
    approve.add_argument("disc_code")

    subparsers.add_parser("web")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings()
    configure_logging(settings)
    logger.info("command started: %s", args.command)
    conn = connect(settings.db_path)

    if args.command == "init-db":
        init_db(conn)
        print(f"Initialized database at {settings.db_path}")
        return

    init_db(conn)

    if args.command == "backup-db":
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = (
            Path(args.output).expanduser()
            if args.output
            else settings.backups_dir / f"archive-{timestamp}.db"
        )
        backup_path = _backup_db(conn, settings.db_path, output_path)
        if not args.output:
            _prune_old_backups(settings.backups_dir, settings.backup_keep)
        print(f"Database backup created at {backup_path}")
        return

    if args.command == "scan":
        result = run_scan_cycle(conn, settings)
        print(result.message)
        return

    if args.command == "status":
        summary = status_summary(conn)
        print("File counts by status:")
        for status, count in sorted(summary["counts"].items()):
            print(f"  {status}: {count}")
        print(f"Pending bytes: {_format_bytes(int(summary['pending_bytes']))}")
        planned_disc = summary["planned_disc"]
        if planned_disc is None:
            print("No planned disc.")
        else:
            print(
                f"Planned disc: {planned_disc['disc_code']} "
                f"({planned_disc['status']}, {_format_bytes(planned_disc['planned_bytes'])}, "
                f"{planned_disc['file_count']} files)"
            )
        return

    if args.command == "plan":
        result = plan_disc(conn, settings)
        if result.disc_code is None:
            print("No files available for planning.")
            return
        print(
            f"Planned {result.disc_code}: "
            f"{result.file_count} files, {_format_bytes(result.total_bytes)}"
        )
        return

    if args.command == "approve":
        ok = approve_disc(conn, args.disc_code)
        if not ok:
            raise SystemExit(f"Disc not found: {args.disc_code}")
        send_notification(settings, "Archiver: disc approved", f"{args.disc_code} is ready for staging")
        print(f"Approved {args.disc_code}")
        return

    if args.command == "stage":
        result = stage_disc(conn, settings, args.disc_code)
        send_notification(settings, "Archiver: staging ready", f"{result.disc_code} staged in {result.stage_dir}")
        print(f"Staged {result.disc_code} at {result.stage_dir} ({result.file_count} files)")
        return

    if args.command == "burn":
        result = burn_disc(conn, settings, args.disc_code)
        if result.verified:
            send_notification(settings, "Archiver: burn and verify complete", f"{result.disc_code} verified successfully")
            print(f"Burned and verified {result.disc_code} from {result.iso_path}")
        else:
            send_notification(settings, "Archiver: burn complete", f"{result.disc_code} burned; verify pending")
            print(f"Burned {result.disc_code} from {result.iso_path}")
            if settings.auto_verify:
                print(f"Automatic verify did not complete: {result.verify_error or 'unknown reason'}")
        return

    if args.command == "burn-worker":
        status_file = settings.db_path.parent / "web-status.json"

        def persist_burn_status(status: dict[str, object]) -> None:
            payload = load_status_payload(status_file, settings)
            payload["burn_status"] = status
            save_status_payload(status_file, payload)

        persist_burn_status({
            "state": "running",
            "disc_code": args.disc_code,
            "progress_percent": None,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "message": f"Nagrywanie wystartowalo dla {args.disc_code}.",
        })

        def burn_progress_callback(disc_code: str, message: str, progress_percent: float | None) -> None:
            payload = load_status_payload(status_file, settings)
            current = payload.get("burn_status", {})
            persist_burn_status({
                "state": "running",
                "disc_code": disc_code,
                "progress_percent": progress_percent,
                "started_at": current.get("started_at"),
                "finished_at": None,
                "message": message,
            })

        try:
            result = burn_disc(conn, settings, args.disc_code, progress_callback=burn_progress_callback)
            final_state = "completed" if not result.verify_error else "completed_with_verify_warning"
            final_message = (
                f"Nagrywanie zakonczone dla {result.disc_code}."
                if not result.verify_error
                else f"Nagrywanie zakonczone dla {result.disc_code}. Verify: {result.verify_error}"
            )
            payload = load_status_payload(status_file, settings)
            current = payload.get("burn_status", {})
            persist_burn_status({
                "state": final_state,
                "disc_code": result.disc_code,
                "progress_percent": 100.0,
                "started_at": current.get("started_at"),
                "finished_at": datetime.now(UTC).isoformat(),
                "message": final_message,
            })
            if result.verified:
                send_notification(settings, "Archiver: burn and verify complete", f"{result.disc_code} verified successfully")
            else:
                send_notification(settings, "Archiver: burn complete", f"{result.disc_code} burned; verify pending")
            return
        except Exception as exc:
            payload = load_status_payload(status_file, settings)
            current = payload.get("burn_status", {})
            persist_burn_status({
                "state": "failed",
                "disc_code": args.disc_code,
                "progress_percent": current.get("progress_percent"),
                "started_at": current.get("started_at"),
                "finished_at": datetime.now(UTC).isoformat(),
                "message": str(exc),
            })
            logger.exception("background burn worker failed")
            raise

    if args.command == "verify":
        mount_path = Path(args.mount_path) if args.mount_path else None
        result = verify_disc(conn, settings, args.disc_code, mount_path=mount_path)
        backup_path = _auto_backup_after_verify(conn, settings, result.disc_code)
        send_notification(settings, "Archiver: verify complete", f"{result.disc_code} verified successfully")
        print(f"Verified {result.disc_code}: {result.checked_files} files")
        if backup_path is not None:
            print(f"Database backup created at {backup_path}")
        return

    if args.command == "speed-verify":
        result = speed_verify_disc_from_device(conn, settings, args.disc_code)
        backup_path = _auto_backup_after_verify(conn, settings, result.disc_code)
        send_notification(settings, "Archiver: speed verify complete", f"{result.disc_code} verified successfully")
        print(f"Speed-verified {result.disc_code}: {result.checked_files} files")
        if backup_path is not None:
            print(f"Database backup created at {backup_path}")
        return

    if args.command == "verify-worker":
        status_file = settings.db_path.parent / "web-status.json"
        verify_progress_step = 10

        def persist_verify_status(status: dict[str, object]) -> None:
            payload = load_status_payload(status_file, settings)
            payload["verify_status"] = status
            save_status_payload(status_file, payload)

        persist_verify_status({
            "state": "running",
            "disc_code": args.disc_code,
            "progress_percent": None,
            "verified_files": 0,
            "total_files": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "message": f"Test ISO wystartowal dla {args.disc_code}. Odczytuje dane bezposrednio z plyty.",
        })

        def verify_progress_callback(disc_code: str, verified_files: int, total_files: int) -> None:
            if verified_files % verify_progress_step != 0 and verified_files != total_files:
                return
            payload = load_status_payload(status_file, settings)
            current = payload.get("verify_status", {})
            progress_percent = (verified_files / total_files * 100.0) if total_files else None
            persist_verify_status({
                "state": "running",
                "disc_code": disc_code,
                "progress_percent": progress_percent,
                "verified_files": verified_files,
                "total_files": total_files,
                "started_at": current.get("started_at"),
                "finished_at": None,
                "message": f"Test ISO {disc_code}: {verified_files}/{total_files}",
            })
        try:
            result = verify_disc_from_device(conn, settings, args.disc_code, progress_callback=verify_progress_callback)
            backup_path = _auto_backup_after_verify(conn, settings, result.disc_code)
            payload = load_status_payload(status_file, settings)
            current = payload.get("verify_status", {})
            persist_verify_status({
                "state": "verified",
                "disc_code": result.disc_code,
                "progress_percent": 100.0,
                "verified_files": result.checked_files,
                "total_files": result.checked_files,
                "started_at": current.get("started_at"),
                "finished_at": datetime.now(UTC).isoformat(),
                "message": (
                    f"Test ISO zakonczony dla {result.disc_code}. Backup: {backup_path.name}"
                    if backup_path is not None
                    else f"Test ISO zakonczony dla {result.disc_code}."
                ),
            })
            send_notification(settings, "Archiver: verify complete", f"{result.disc_code} verified successfully")
            print(f"Verified {result.disc_code}: {result.checked_files} files")
            if backup_path is not None:
                print(f"Database backup created at {backup_path}")
            return
        except Exception as exc:
            payload = load_status_payload(status_file, settings)
            current = payload.get("verify_status", {})
            persist_verify_status({
                "state": "verify_failed",
                "disc_code": args.disc_code,
                "progress_percent": current.get("progress_percent"),
                "verified_files": current.get("verified_files", 0),
                "total_files": current.get("total_files", 0),
                "started_at": current.get("started_at"),
                "finished_at": datetime.now(UTC).isoformat(),
                "message": str(exc),
            })
            logger.exception("background verify worker failed")
            raise

    if args.command == "speed-verify-worker":
        status_file = settings.db_path.parent / "web-status.json"
        verify_progress_step = 10

        def persist_verify_status(status: dict[str, object]) -> None:
            payload = load_status_payload(status_file, settings)
            payload["verify_status"] = status
            save_status_payload(status_file, payload)

        persist_verify_status({
            "state": "running",
            "disc_code": args.disc_code,
            "progress_percent": None,
            "verified_files": 0,
            "total_files": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "message": (
                f"Test plik po pliku wystartowal dla {args.disc_code}. "
                "Liczy hashe bez zapisywania plikow tymczasowych na dysku."
            ),
        })

        def verify_progress_callback(disc_code: str, verified_files: int, total_files: int) -> None:
            if verified_files % verify_progress_step != 0 and verified_files != total_files:
                return
            payload = load_status_payload(status_file, settings)
            current = payload.get("verify_status", {})
            progress_percent = (verified_files / total_files * 100.0) if total_files else None
            persist_verify_status({
                "state": "running",
                "disc_code": disc_code,
                "progress_percent": progress_percent,
                "verified_files": verified_files,
                "total_files": total_files,
                "started_at": current.get("started_at"),
                "finished_at": None,
                "message": f"Test plik po pliku {disc_code}: {verified_files}/{total_files}",
            })

        try:
            result = speed_verify_disc_from_device(
                conn,
                settings,
                args.disc_code,
                progress_callback=verify_progress_callback,
            )
            backup_path = _auto_backup_after_verify(conn, settings, result.disc_code)
            payload = load_status_payload(status_file, settings)
            current = payload.get("verify_status", {})
            persist_verify_status({
                "state": "verified",
                "disc_code": result.disc_code,
                "progress_percent": 100.0,
                "verified_files": result.checked_files,
                "total_files": result.checked_files,
                "started_at": current.get("started_at"),
                "finished_at": datetime.now(UTC).isoformat(),
                "message": (
                    f"Test plik po pliku zakonczony dla {result.disc_code}. Backup: {backup_path.name}"
                    if backup_path is not None
                    else f"Test plik po pliku zakonczony dla {result.disc_code}."
                ),
            })
            send_notification(settings, "Archiver: speed verify complete", f"{result.disc_code} verified successfully")
            print(f"Speed-verified {result.disc_code}: {result.checked_files} files")
            if backup_path is not None:
                print(f"Database backup created at {backup_path}")
            return
        except Exception as exc:
            payload = load_status_payload(status_file, settings)
            current = payload.get("verify_status", {})
            persist_verify_status({
                "state": "verify_failed",
                "disc_code": args.disc_code,
                "progress_percent": current.get("progress_percent"),
                "verified_files": current.get("verified_files", 0),
                "total_files": current.get("total_files", 0),
                "started_at": current.get("started_at"),
                "finished_at": datetime.now(UTC).isoformat(),
                "message": str(exc),
            })
            logger.exception("background speed verify worker failed")
            raise

    if args.command == "web":
        app = create_app(conn, settings)
        uvicorn.run(app, host=settings.web_host, port=settings.web_port)
        return

    if args.command == "start":
        app = create_app(conn, settings)
        uvicorn.run(app, host=settings.web_host, port=settings.web_port)
        return

    parser.error("Unknown command")


if __name__ == "__main__":
    main()
