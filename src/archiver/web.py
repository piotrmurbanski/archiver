from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
import logging
import threading
from datetime import UTC, datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .burner import burn_disc, probe_optical_media, stage_disc
from .config import Settings
from .planner import approve_disc, plan_disc, replan_disc
from .repository import active_disc, status_summary
from .status_store import default_status_payload, load_status_payload, save_status_payload
from .workflow import run_scan_cycle

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
logger = logging.getLogger(__name__)

def create_app(conn: sqlite3.Connection, settings: Settings, startup_scan: bool = False) -> FastAPI:
    app = FastAPI(title="Archiver")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    scan_lock = threading.Lock()
    plan_lock = threading.Lock()
    stage_lock = threading.Lock()
    burn_lock = threading.Lock()
    prepare_lock = threading.Lock()
    status_file = settings.db_path.parent / "web-status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    state_lock = threading.Lock()

    defaults = default_status_payload(settings)
    default_scan_status = defaults["scan_status"]
    default_plan_status = defaults["plan_status"]
    default_stage_status = defaults["stage_status"]
    default_prepare_status = defaults["prepare_status"]
    default_burn_status = defaults["burn_status"]
    default_verify_status = defaults["verify_status"]
    default_media_probe = defaults["media_probe"]
    default_root_checks = defaults["root_checks"]
    app.state.scan_status = dict(default_scan_status)
    app.state.plan_status = dict(default_plan_status)
    app.state.stage_status = dict(default_stage_status)
    app.state.prepare_status = dict(default_prepare_status)
    app.state.burn_status = dict(default_burn_status)
    app.state.verify_status = dict(default_verify_status)
    app.state.media_probe = default_media_probe
    app.state.root_checks = [dict(item) for item in default_root_checks]

    def save_status_snapshot() -> None:
        payload = {
            "scan_status": app.state.scan_status,
            "plan_status": app.state.plan_status,
            "stage_status": app.state.stage_status,
            "prepare_status": app.state.prepare_status,
            "burn_status": app.state.burn_status,
            "verify_status": app.state.verify_status,
            "media_probe": app.state.media_probe,
            "root_checks": app.state.root_checks,
        }
        with state_lock:
            save_status_payload(status_file, payload)

    def load_status_snapshot() -> None:
        payload = load_status_payload(status_file, settings)
        app.state.scan_status = payload["scan_status"]
        app.state.plan_status = payload["plan_status"]
        app.state.stage_status = payload["stage_status"]
        app.state.prepare_status = payload["prepare_status"]
        app.state.burn_status = payload["burn_status"]
        app.state.verify_status = payload["verify_status"]
        app.state.media_probe = payload.get("media_probe", default_media_probe)
        app.state.root_checks = payload["root_checks"]
        for status in (
            app.state.scan_status,
            app.state.plan_status,
            app.state.stage_status,
            app.state.prepare_status,
        ):
            if status.get("state") == "running":
                status["state"] = "interrupted"
                status["finished_at"] = datetime.now(UTC).isoformat()
                status["message"] = "Proces zostal przerwany przez restart aplikacji."
        save_status_snapshot()

    def reload_status_snapshot() -> None:
        payload = load_status_payload(status_file, settings)
        app.state.scan_status = payload["scan_status"]
        app.state.plan_status = payload["plan_status"]
        app.state.stage_status = payload["stage_status"]
        app.state.prepare_status = payload["prepare_status"]
        app.state.burn_status = payload["burn_status"]
        app.state.verify_status = payload["verify_status"]
        app.state.media_probe = payload.get("media_probe", default_media_probe)
        app.state.root_checks = payload["root_checks"]

    def update_scan_status(status: dict[str, object]) -> None:
        app.state.scan_status = status
        save_status_snapshot()

    def update_plan_status(status: dict[str, object]) -> None:
        app.state.plan_status = status
        save_status_snapshot()

    def update_stage_status(status: dict[str, object]) -> None:
        app.state.stage_status = status
        save_status_snapshot()

    def update_prepare_status(status: dict[str, object]) -> None:
        app.state.prepare_status = status
        save_status_snapshot()

    def update_burn_status(status: dict[str, object]) -> None:
        app.state.burn_status = status
        save_status_snapshot()

    def update_verify_status(status: dict[str, object]) -> None:
        app.state.verify_status = status
        save_status_snapshot()

    def update_media_probe(media_probe: dict[str, object] | None) -> None:
        app.state.media_probe = media_probe
        save_status_snapshot()

    def update_root_checks(root_checks: list[dict[str, object]]) -> None:
        app.state.root_checks = root_checks
        save_status_snapshot()

    load_status_snapshot()

    def workflow_busy_message() -> str | None:
        if app.state.scan_status["state"] == "running":
            return "Trwa skan. Poczekaj na jego zakonczenie."
        if app.state.plan_status["state"] == "running":
            return "Trwa planowanie plyty. Poczekaj na jego zakonczenie."
        if app.state.stage_status["state"] == "running":
            return "Trwa stage. Poczekaj na jego zakonczenie."
        if app.state.burn_status["state"] == "running":
            return "Trwa nagrywanie. Poczekaj na jego zakonczenie."
        if app.state.verify_status["state"] == "running":
            return "Trwa verify. Poczekaj na jego zakonczenie."
        if app.state.prepare_status["state"] == "running":
            return "Trwa zlozony workflow. Poczekaj na jego zakonczenie."
        return None

    def set_scan_message(message: str) -> None:
        update_scan_status({
            "state": "failed",
            "started_at": app.state.scan_status["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "current_root": app.state.scan_status["current_root"],
            "scanned_files": app.state.scan_status["scanned_files"],
            "new_files": app.state.scan_status["new_files"],
            "changed_files": app.state.scan_status["changed_files"],
            "message": message,
        })

    def set_plan_message(message: str, disc_code: str | None = None) -> None:
        update_plan_status({
            "state": "failed",
            "disc_code": disc_code if disc_code is not None else app.state.plan_status["disc_code"],
            "hashed_files": app.state.plan_status["hashed_files"],
            "total_files": app.state.plan_status["total_files"],
            "started_at": app.state.plan_status["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "message": message,
        })

    def set_stage_message(message: str, disc_code: str | None = None) -> None:
        update_stage_status({
            "state": "failed",
            "disc_code": disc_code if disc_code is not None else app.state.stage_status["disc_code"],
            "copied_files": app.state.stage_status["copied_files"],
            "total_files": app.state.stage_status["total_files"],
            "started_at": app.state.stage_status["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "message": message,
        })

    def set_prepare_message(message: str) -> None:
        update_prepare_status({
            "state": "failed",
            "started_at": app.state.prepare_status["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "message": message,
        })

    def set_burn_message(message: str, disc_code: str | None = None) -> None:
        update_burn_status({
            "state": "failed",
            "disc_code": disc_code if disc_code is not None else app.state.burn_status["disc_code"],
            "progress_percent": app.state.burn_status["progress_percent"],
            "started_at": app.state.burn_status["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "message": message,
        })

    def set_verify_message(message: str, disc_code: str | None = None) -> None:
        update_verify_status({
            "state": "failed",
            "disc_code": disc_code if disc_code is not None else app.state.verify_status["disc_code"],
            "progress_percent": app.state.verify_status["progress_percent"],
            "verified_files": app.state.verify_status["verified_files"],
            "total_files": app.state.verify_status["total_files"],
            "started_at": app.state.verify_status["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "message": message,
        })

    def plan_progress_callback(disc_code: str, hashed_files: int, total_files: int) -> None:
        update_plan_status({
            "state": "running",
            "disc_code": disc_code,
            "hashed_files": hashed_files,
            "total_files": total_files,
            "started_at": app.state.plan_status["started_at"],
            "finished_at": None,
            "message": f"Hashing files for {disc_code}: {hashed_files}/{total_files}",
        })

    def scan_progress_callback(current_root: str | None, stats) -> None:
        update_scan_status({
            "state": "running",
            "started_at": app.state.scan_status["started_at"],
            "finished_at": None,
            "current_root": current_root,
            "scanned_files": stats.scanned_files,
            "new_files": stats.new_files,
            "changed_files": stats.changed_files,
            "message": (
                f"Scanning {current_root or 'done'}: "
                f"{stats.scanned_files} scanned, "
                f"{stats.new_files} new, "
                f"{stats.changed_files} changed"
            ),
        })

    def stage_progress_callback(disc_code: str, copied_files: int, total_files: int) -> None:
        update_stage_status({
            "state": "running",
            "disc_code": disc_code,
            "copied_files": copied_files,
            "total_files": total_files,
            "started_at": app.state.stage_status["started_at"],
            "finished_at": None,
            "message": f"Copying files for {disc_code}: {copied_files}/{total_files}",
        })

    def burn_progress_callback(disc_code: str, message: str, progress_percent: float | None) -> None:
        update_burn_status({
            "state": "running",
            "disc_code": disc_code,
            "progress_percent": progress_percent,
            "started_at": app.state.burn_status["started_at"],
            "finished_at": None,
            "message": message,
        })

    def resolve_planning_limit(plan_target: str) -> tuple[int, str]:
        if plan_target == "media":
            try:
                probe = probe_optical_media(settings)
            except Exception as exc:
                logger.warning("media-based planning unavailable, falling back to profile: %s", exc)
                return settings.planning_limit_bytes, "profile-fallback"
            update_media_probe({
                "writable_bytes": probe.writable_bytes,
                "writable_blocks": probe.writable_blocks,
                "media_current": probe.media_current,
                "media_status": probe.media_status,
                "media_summary": probe.media_summary,
            })
            return int(probe.writable_bytes * settings.fill_ratio), "media"
        return settings.planning_limit_bytes, "profile"

    def start_scan_job(trigger: str) -> bool:
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_scan_message(busy_message)
            return False
        if not scan_lock.acquire(blocking=False):
            set_scan_message("Skan juz trwa.")
            return False

        def worker() -> None:
            update_scan_status({
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "current_root": None,
                "scanned_files": 0,
                "new_files": 0,
                "changed_files": 0,
                "message": f"Scan started by {trigger}.",
            })
            try:
                result = run_scan_cycle(
                    conn,
                    settings,
                    plan_progress_callback=plan_progress_callback,
                    scan_progress_callback=scan_progress_callback,
                )
                update_root_checks(
                    [{"root": str(root), "available": True} for root in settings.roots]
                )
                update_scan_status({
                    "state": "completed",
                    "started_at": app.state.scan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "current_root": app.state.scan_status["current_root"],
                    "scanned_files": app.state.scan_status["scanned_files"],
                    "new_files": app.state.scan_status["new_files"],
                    "changed_files": app.state.scan_status["changed_files"],
                    "message": result.message,
                })
            except Exception as exc:
                logger.exception("background scan failed")
                update_scan_status({
                    "state": "failed",
                    "started_at": app.state.scan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "current_root": app.state.scan_status["current_root"],
                    "scanned_files": app.state.scan_status["scanned_files"],
                    "new_files": app.state.scan_status["new_files"],
                    "changed_files": app.state.scan_status["changed_files"],
                    "message": str(exc),
                })
            finally:
                scan_lock.release()

        threading.Thread(target=worker, name="archiver-scan", daemon=True).start()
        return True

    def start_plan_job(trigger: str, plan_target: str = "media") -> bool:
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_plan_message(busy_message)
            return False
        if not plan_lock.acquire(blocking=False):
            set_plan_message("Planowanie juz trwa.")
            return False

        def worker() -> None:
            update_plan_status({
                "state": "running",
                "disc_code": None,
                "hashed_files": 0,
                "total_files": 0,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Planning started by {trigger}.",
            })
            try:
                planning_limit_bytes, resolved_target = resolve_planning_limit(plan_target)
                result = plan_disc(
                    conn,
                    settings,
                    progress_callback=plan_progress_callback,
                    planning_limit_bytes=planning_limit_bytes,
                )
                if result.disc_code is None:
                    update_plan_status({
                        "state": "completed",
                        "disc_code": None,
                        "hashed_files": 0,
                        "total_files": 0,
                        "started_at": app.state.plan_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": f"No files available for planning ({resolved_target}).",
                    })
                else:
                    update_plan_status({
                        "state": "completed",
                        "disc_code": result.disc_code,
                        "hashed_files": result.file_count,
                        "total_files": result.file_count,
                        "started_at": app.state.plan_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": f"Planned {result.disc_code}: {result.file_count} files ({resolved_target}).",
                    })
            except Exception as exc:
                logger.exception("background plan failed")
                update_plan_status({
                    "state": "failed",
                    "disc_code": app.state.plan_status["disc_code"],
                    "hashed_files": app.state.plan_status["hashed_files"],
                    "total_files": app.state.plan_status["total_files"],
                    "started_at": app.state.plan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                })
            finally:
                plan_lock.release()

        threading.Thread(target=worker, name="archiver-plan", daemon=True).start()
        return True

    def start_replan_job(trigger: str, disc_code: str, plan_target: str = "media") -> bool:
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_plan_message(busy_message, disc_code)
            return False
        if not plan_lock.acquire(blocking=False):
            set_plan_message("Planowanie juz trwa.", disc_code)
            return False

        def worker() -> None:
            update_plan_status({
                "state": "running",
                "disc_code": disc_code,
                "hashed_files": 0,
                "total_files": 0,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Replanning started by {trigger} for {disc_code}.",
            })
            try:
                planning_limit_bytes, resolved_target = resolve_planning_limit(plan_target)
                result = replan_disc(
                    conn,
                    settings,
                    disc_code,
                    progress_callback=plan_progress_callback,
                    planning_limit_bytes=planning_limit_bytes,
                )
                update_plan_status({
                    "state": "completed",
                    "disc_code": result.disc_code,
                    "hashed_files": result.file_count,
                    "total_files": result.file_count,
                    "started_at": app.state.plan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Replanned {result.disc_code}: {result.file_count} files ({resolved_target}).",
                })
            except Exception as exc:
                logger.exception("background replan failed")
                update_plan_status({
                    "state": "failed",
                    "disc_code": disc_code,
                    "hashed_files": app.state.plan_status["hashed_files"],
                    "total_files": app.state.plan_status["total_files"],
                    "started_at": app.state.plan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                })
            finally:
                plan_lock.release()

        threading.Thread(target=worker, name="archiver-replan", daemon=True).start()
        return True

    def start_stage_job(trigger: str, disc_code: str) -> bool:
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_stage_message(busy_message, disc_code)
            return False
        if not stage_lock.acquire(blocking=False):
            set_stage_message("Stage juz trwa.", disc_code)
            return False

        def worker() -> None:
            update_stage_status({
                "state": "running",
                "disc_code": disc_code,
                "copied_files": 0,
                "total_files": 0,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Staging started by {trigger}.",
            })
            try:
                result = stage_disc(conn, settings, disc_code, progress_callback=stage_progress_callback)
                update_stage_status({
                    "state": "completed",
                    "disc_code": result.disc_code,
                    "copied_files": result.file_count,
                    "total_files": result.file_count,
                    "started_at": app.state.stage_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Staged {result.disc_code}: {result.file_count} files.",
                })
            except Exception as exc:
                logger.exception("background stage failed")
                update_stage_status({
                    "state": "failed",
                    "disc_code": disc_code,
                    "copied_files": app.state.stage_status["copied_files"],
                    "total_files": app.state.stage_status["total_files"],
                    "started_at": app.state.stage_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                })
            finally:
                stage_lock.release()

        threading.Thread(target=worker, name="archiver-stage", daemon=True).start()
        return True

    def start_prepare_job(trigger: str) -> bool:
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_prepare_message(busy_message)
            return False
        if not prepare_lock.acquire(blocking=False):
            set_prepare_message("Zlozony workflow juz trwa.")
            return False

        def worker() -> None:
            update_prepare_status({
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Prepare workflow started by {trigger}.",
            })
            try:
                disc = active_disc(conn)
                if disc is None:
                    scan_result = run_scan_cycle(conn, settings, plan_progress_callback=plan_progress_callback)
                    update_root_checks(
                        [{"root": str(root), "available": not scan_result.skipped} for root in settings.roots]
                    )
                    app.state.prepare_status["message"] = scan_result.message
                    save_status_snapshot()
                    disc = active_disc(conn)

                if disc is None:
                    update_prepare_status({
                        "state": "completed",
                        "started_at": app.state.prepare_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": "No disc ready after scan/plan. Threshold may not be reached yet.",
                    })
                    return

                disc_code = disc["disc_code"]
                disc_status = disc["status"]
                if disc_status == "planned":
                    approve_disc(conn, disc_code)
                    disc_status = "approved"
                if disc_status == "approved":
                    stage_result = stage_disc(conn, settings, disc_code, progress_callback=stage_progress_callback)
                    update_stage_status({
                        "state": "completed",
                        "disc_code": stage_result.disc_code,
                        "copied_files": stage_result.file_count,
                        "total_files": stage_result.file_count,
                        "started_at": app.state.stage_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": f"Staged {stage_result.disc_code}: {stage_result.file_count} files.",
                    })
                    disc_status = "staged"

                update_prepare_status({
                    "state": "completed",
                    "started_at": app.state.prepare_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Workflow complete. {disc_code} is now {disc_status}.",
                })
            except Exception as exc:
                logger.exception("combined workflow failed")
                update_prepare_status({
                    "state": "failed",
                    "started_at": app.state.prepare_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                })
            finally:
                prepare_lock.release()

        threading.Thread(target=worker, name="archiver-prepare", daemon=True).start()
        return True

    def start_burn_job(trigger: str, disc_code: str) -> bool:
        reload_status_snapshot()
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_burn_message(busy_message, disc_code)
            return False
        unit_name = f"archiver-burn-{disc_code.lower()}"
        update_burn_status({
            "state": "running",
            "disc_code": disc_code,
            "progress_percent": None,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "message": f"Nagrywanie wystartowalo przez {trigger}.",
        })
        update_verify_status({
            "state": "idle",
            "disc_code": disc_code,
            "progress_percent": None,
            "verified_files": 0,
            "total_files": 0,
            "started_at": None,
            "finished_at": None,
            "message": "Verify jeszcze nie wystartowalo dla tej plyty.",
        })
        command = [
            "systemd-run",
            "--user",
            "--unit",
            unit_name,
            "--collect",
            f"--working-directory={Path.cwd()}",
            str(Path.cwd() / ".venv/bin/archiver"),
            "burn-worker",
            disc_code,
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            error_text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            set_burn_message(error_text or "Nie udalo sie uruchomic joba nagrywania.", disc_code)
            return False
        return True

    def start_verify_job(trigger: str, disc_code: str, speed: bool = False) -> bool:
        reload_status_snapshot()
        busy_message = workflow_busy_message()
        if busy_message is not None:
            set_verify_message(busy_message, disc_code)
            return False
        unit_name = f"archiver-{'speed-verify' if speed else 'verify'}-{disc_code.lower()}"
        action_label = "Test plik po pliku" if speed else "Test ISO"
        worker_command = "speed-verify-worker" if speed else "verify-worker"
        update_verify_status({
            "state": "running",
            "disc_code": disc_code,
            "progress_percent": None,
            "verified_files": 0,
            "total_files": 0,
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "message": (
                f"{action_label} wystartowalo przez {trigger}. "
                + (
                    "Liczy hashe bez zapisywania plikow tymczasowych na dysku."
                    if speed
                    else "Wsun plyte, aplikacja odczyta ja bezposrednio z napedu."
                )
            ),
        })
        command = [
            "systemd-run",
            "--user",
            "--unit",
            unit_name,
            "--collect",
            f"--working-directory={Path.cwd()}",
            str(Path.cwd() / ".venv/bin/archiver"),
            worker_command,
            disc_code,
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            error_text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            set_verify_message(error_text or "Nie udalo sie uruchomic joba verify.", disc_code)
            return False
        return True

    if startup_scan:
        @app.on_event("startup")
        def _startup_scan() -> None:
            start_scan_job("startup")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        reload_status_snapshot()
        summary = status_summary(conn)
        workflow_busy = any(
            status["state"] == "running"
            for status in (
                app.state.scan_status,
                app.state.plan_status,
                app.state.stage_status,
                app.state.burn_status,
                app.state.verify_status,
                app.state.prepare_status,
            )
        )
        media_probe = app.state.media_probe
        if (
            settings.optical_device
            and app.state.burn_status["state"] != "running"
            and media_probe is None
        ):
            try:
                probe = probe_optical_media(settings)
                media_probe = {
                    "writable_bytes": probe.writable_bytes,
                    "writable_blocks": probe.writable_blocks,
                    "media_current": probe.media_current,
                    "media_status": probe.media_status,
                    "media_summary": probe.media_summary,
                }
                update_media_probe(media_probe)
            except Exception:
                media_probe = None
        auto_refresh = workflow_busy
        disc = summary["planned_disc"]
        wizard = {
            "heading": "Następny krok",
            "description": "Brak aktywnej partii. Zacznij od planowania.",
            "action_path": "/plan",
            "action_label": "Zaplanuj",
            "disc_code": None,
            "show": True,
            "disabled": workflow_busy,
            "error": None,
            "hint": None,
            "secondary_actions": [],
        }
        if app.state.plan_status["state"] == "failed" and disc is None:
            wizard.update({
                "description": "Planowanie nie powiodło się.",
                "action_path": "/plan",
                "action_label": "Spróbuj planowania ponownie",
                "error": app.state.plan_status["message"],
            })
        elif disc is not None:
            disc_status = disc["status"]
            wizard["disc_code"] = disc["disc_code"]
            if disc_status in {"planned", "approved"}:
                wizard.update({
                    "description": f"{disc['disc_code']} jest gotowa do stage.",
                    "action_path": "/stage",
                    "action_label": "Stage",
                })
            elif disc_status == "staged":
                wizard.update({
                    "description": f"{disc['disc_code']} jest przygotowana do nagrania.",
                    "action_path": "/burn",
                    "action_label": "Nagraj",
                })
            elif disc_status == "burn_failed":
                wizard.update({
                    "description": f"Nagrywanie {disc['disc_code']} nie powiodło się.",
                    "action_path": "/burn",
                    "action_label": "Nagraj ponownie",
                    "error": app.state.burn_status["message"],
                })
            elif disc_status in {"burned", "verify_failed"}:
                wizard.update({
                    "description": f"{disc['disc_code']} czeka na test poprawnosci nagrania.",
                    "action_path": "/verify",
                    "action_label": "Test ISO",
                    "hint": "Po nagraniu wsun płytę i uruchom test ISO albo test plik po pliku.",
                    "secondary_actions": [
                        {
                            "path": "/speed-verify",
                            "label": "Test plik po pliku",
                            "disc_code": disc["disc_code"],
                        }
                    ],
                })
                if disc_status == "verify_failed":
                    wizard["action_label"] = "Test ISO ponownie"
                    wizard["error"] = app.state.verify_status["message"]
            else:
                wizard.update({
                    "description": f"Bieżący status płyty: {disc_status}",
                    "show": False,
                })
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "summary": summary,
                "settings": settings,
                "root_checks": app.state.root_checks,
                "scan_status": app.state.scan_status,
                "plan_status": app.state.plan_status,
                "stage_status": app.state.stage_status,
                "burn_status": app.state.burn_status,
                "prepare_status": app.state.prepare_status,
                "media_probe": media_probe,
                "verify_status": app.state.verify_status,
                "workflow_busy": workflow_busy,
                "wizard": wizard,
                "auto_refresh": auto_refresh,
                "auto_refresh_seconds": 4,
            },
        )

    @app.post("/plan")
    def plan(plan_target: str = Form("media")):
        reload_status_snapshot()
        start_plan_job("web", plan_target=plan_target)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/replan")
    def replan(disc_code: str = Form(...), plan_target: str = Form("media")):
        reload_status_snapshot()
        start_replan_job("web", disc_code, plan_target=plan_target)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/approve")
    def approve(disc_code: str = Form(...)):
        reload_status_snapshot()
        approve_disc(conn, disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/stage")
    def stage(disc_code: str = Form(...)):
        reload_status_snapshot()
        start_stage_job("web", disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/burn")
    def burn(disc_code: str = Form(...)):
        reload_status_snapshot()
        start_burn_job("web", disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/verify")
    def verify(disc_code: str = Form(...)):
        reload_status_snapshot()
        start_verify_job("web", disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/speed-verify")
    def speed_verify(disc_code: str = Form(...)):
        reload_status_snapshot()
        start_verify_job("web", disc_code, speed=True)
        return RedirectResponse(url="/", status_code=303)

    @app.api_route("/burn", methods=["GET", "HEAD"])
    def burn_get_redirect():
        return RedirectResponse(url="/", status_code=303)

    @app.post("/scan")
    def scan():
        reload_status_snapshot()
        start_scan_job("web")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/prepare")
    def prepare():
        reload_status_snapshot()
        start_prepare_job("web")
        return RedirectResponse(url="/", status_code=303)

    return app
