from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .burner import burn_disc, stage_disc, verify_disc
from .config import load_settings
from .db import connect, init_db
from .logging_setup import configure_logging
from .notifier import send_notification
from .planner import approve_disc, plan_disc
from .repository import status_summary
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archiver")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")
    subparsers.add_parser("scan")
    subparsers.add_parser("start")
    subparsers.add_parser("status")
    subparsers.add_parser("plan")
    stage = subparsers.add_parser("stage")
    stage.add_argument("disc_code")
    burn = subparsers.add_parser("burn")
    burn.add_argument("disc_code")
    verify = subparsers.add_parser("verify")
    verify.add_argument("disc_code")
    verify.add_argument("--mount-path", default=None)

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

    if args.command == "verify":
        mount_path = Path(args.mount_path) if args.mount_path else None
        result = verify_disc(conn, settings, args.disc_code, mount_path=mount_path)
        send_notification(settings, "Archiver: verify complete", f"{result.disc_code} verified successfully")
        print(f"Verified {result.disc_code}: {result.checked_files} files")
        return

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
