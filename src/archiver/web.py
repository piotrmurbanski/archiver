from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import logging
import threading
from datetime import UTC, datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .burner import burn_disc, stage_disc
from .config import Settings
from .planner import approve_disc, plan_disc, replan_disc
from .repository import active_disc, status_summary
from .scanner import root_is_available
from .workflow import run_scan_cycle

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
logger = logging.getLogger(__name__)

def create_app(conn: sqlite3.Connection, settings: Settings, startup_scan: bool = False) -> FastAPI:
    app = FastAPI(title="Archiver")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    scan_lock = threading.Lock()
    plan_lock = threading.Lock()
    stage_lock = threading.Lock()
    prepare_lock = threading.Lock()
    status_file = settings.db_path.parent / "web-status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    state_lock = threading.Lock()

    default_scan_status = {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "current_root": None,
        "scanned_files": 0,
        "new_files": 0,
        "changed_files": 0,
        "message": "No scan started yet.",
    }
    default_plan_status = {
        "state": "idle",
        "disc_code": None,
        "hashed_files": 0,
        "total_files": 0,
        "started_at": None,
        "finished_at": None,
        "message": "No planning started yet.",
    }
    default_stage_status = {
        "state": "idle",
        "disc_code": None,
        "copied_files": 0,
        "total_files": 0,
        "started_at": None,
        "finished_at": None,
        "message": "No staging started yet.",
    }
    default_prepare_status = {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "message": "No combined workflow started yet.",
    }
    app.state.scan_status = dict(default_scan_status)
    app.state.plan_status = dict(default_plan_status)
    app.state.stage_status = dict(default_stage_status)
    app.state.prepare_status = dict(default_prepare_status)

    def save_status_snapshot() -> None:
        payload = {
            "scan_status": app.state.scan_status,
            "plan_status": app.state.plan_status,
            "stage_status": app.state.stage_status,
            "prepare_status": app.state.prepare_status,
        }
        with state_lock:
            status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_status_snapshot() -> None:
        if not status_file.exists():
            return
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to load persisted web status")
            return
        app.state.scan_status = {**default_scan_status, **payload.get("scan_status", {})}
        app.state.plan_status = {**default_plan_status, **payload.get("plan_status", {})}
        app.state.stage_status = {**default_stage_status, **payload.get("stage_status", {})}
        app.state.prepare_status = {**default_prepare_status, **payload.get("prepare_status", {})}
        for status in (app.state.scan_status, app.state.plan_status, app.state.stage_status, app.state.prepare_status):
            if status.get("state") == "running":
                status["state"] = "interrupted"
                status["finished_at"] = datetime.now(UTC).isoformat()
                status["message"] = "Proces zostal przerwany przez restart aplikacji."
        save_status_snapshot()

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

    load_status_snapshot()

    def workflow_busy_message() -> str | None:
        if app.state.scan_status["state"] == "running":
            return "Trwa skan. Poczekaj na jego zakonczenie."
        if app.state.plan_status["state"] == "running":
            return "Trwa planowanie plyty. Poczekaj na jego zakonczenie."
        if app.state.stage_status["state"] == "running":
            return "Trwa stage. Poczekaj na jego zakonczenie."
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

    def start_plan_job(trigger: str) -> bool:
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
                result = plan_disc(conn, settings, progress_callback=plan_progress_callback)
                if result.disc_code is None:
                    update_plan_status({
                        "state": "completed",
                        "disc_code": None,
                        "hashed_files": 0,
                        "total_files": 0,
                        "started_at": app.state.plan_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": "No files available for planning.",
                    })
                else:
                    update_plan_status({
                        "state": "completed",
                        "disc_code": result.disc_code,
                        "hashed_files": result.file_count,
                        "total_files": result.file_count,
                        "started_at": app.state.plan_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": f"Planned {result.disc_code}: {result.file_count} files.",
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

    def start_replan_job(trigger: str, disc_code: str) -> bool:
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
                result = replan_disc(conn, settings, disc_code, progress_callback=plan_progress_callback)
                update_plan_status({
                    "state": "completed",
                    "disc_code": result.disc_code,
                    "hashed_files": result.file_count,
                    "total_files": result.file_count,
                    "started_at": app.state.plan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Replanned {result.disc_code}: {result.file_count} files.",
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

    if startup_scan:
        @app.on_event("startup")
        def _startup_scan() -> None:
            start_scan_job("startup")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        summary = status_summary(conn)
        auto_refresh = any(
            status["state"] == "running"
            for status in (
                app.state.scan_status,
                app.state.plan_status,
                app.state.stage_status,
                app.state.prepare_status,
            )
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "summary": summary,
                "settings": settings,
                "root_checks": [(str(root), root_is_available(root)) for root in settings.roots],
                "scan_status": app.state.scan_status,
                "plan_status": app.state.plan_status,
                "stage_status": app.state.stage_status,
                "prepare_status": app.state.prepare_status,
                "auto_refresh": auto_refresh,
                "auto_refresh_seconds": 4,
            },
        )

    @app.post("/plan")
    def plan():
        start_plan_job("web")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/replan")
    def replan(disc_code: str = Form(...)):
        start_replan_job("web", disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/approve")
    def approve(disc_code: str = Form(...)):
        approve_disc(conn, disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/stage")
    def stage(disc_code: str = Form(...)):
        start_stage_job("web", disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/burn")
    def burn(disc_code: str = Form(...)):
        burn_disc(conn, settings, disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/scan")
    def scan():
        start_scan_job("web")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/prepare")
    def prepare():
        start_prepare_job("web")
        return RedirectResponse(url="/", status_code=303)

    return app
