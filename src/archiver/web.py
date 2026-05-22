from __future__ import annotations

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
from .planner import approve_disc, plan_disc
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
    app.state.scan_status = {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "message": "No scan started yet.",
    }
    app.state.plan_status = {
        "state": "idle",
        "disc_code": None,
        "hashed_files": 0,
        "total_files": 0,
        "started_at": None,
        "finished_at": None,
        "message": "No planning started yet.",
    }
    app.state.stage_status = {
        "state": "idle",
        "disc_code": None,
        "copied_files": 0,
        "total_files": 0,
        "started_at": None,
        "finished_at": None,
        "message": "No staging started yet.",
    }
    app.state.prepare_status = {
        "state": "idle",
        "started_at": None,
        "finished_at": None,
        "message": "No combined workflow started yet.",
    }

    def plan_progress_callback(disc_code: str, hashed_files: int, total_files: int) -> None:
        app.state.plan_status = {
            "state": "running",
            "disc_code": disc_code,
            "hashed_files": hashed_files,
            "total_files": total_files,
            "started_at": app.state.plan_status["started_at"],
            "finished_at": None,
            "message": f"Hashing files for {disc_code}: {hashed_files}/{total_files}",
        }

    def stage_progress_callback(disc_code: str, copied_files: int, total_files: int) -> None:
        app.state.stage_status = {
            "state": "running",
            "disc_code": disc_code,
            "copied_files": copied_files,
            "total_files": total_files,
            "started_at": app.state.stage_status["started_at"],
            "finished_at": None,
            "message": f"Copying files for {disc_code}: {copied_files}/{total_files}",
        }

    def start_scan_job(trigger: str) -> bool:
        if not scan_lock.acquire(blocking=False):
            return False

        def worker() -> None:
            app.state.scan_status = {
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Scan started by {trigger}.",
            }
            try:
                result = run_scan_cycle(conn, settings, plan_progress_callback=plan_progress_callback)
                app.state.scan_status = {
                    "state": "completed",
                    "started_at": app.state.scan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": result.message,
                }
            except Exception as exc:
                logger.exception("background scan failed")
                app.state.scan_status = {
                    "state": "failed",
                    "started_at": app.state.scan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                }
            finally:
                scan_lock.release()

        threading.Thread(target=worker, name="archiver-scan", daemon=True).start()
        return True

    def start_plan_job(trigger: str) -> bool:
        if not plan_lock.acquire(blocking=False):
            return False

        def worker() -> None:
            app.state.plan_status = {
                "state": "running",
                "disc_code": None,
                "hashed_files": 0,
                "total_files": 0,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Planning started by {trigger}.",
            }
            try:
                result = plan_disc(conn, settings, progress_callback=plan_progress_callback)
                if result.disc_code is None:
                    app.state.plan_status = {
                        "state": "completed",
                        "disc_code": None,
                        "hashed_files": 0,
                        "total_files": 0,
                        "started_at": app.state.plan_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": "No files available for planning.",
                    }
                else:
                    app.state.plan_status = {
                        "state": "completed",
                        "disc_code": result.disc_code,
                        "hashed_files": result.file_count,
                        "total_files": result.file_count,
                        "started_at": app.state.plan_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": f"Planned {result.disc_code}: {result.file_count} files.",
                    }
            except Exception as exc:
                logger.exception("background plan failed")
                app.state.plan_status = {
                    "state": "failed",
                    "disc_code": app.state.plan_status["disc_code"],
                    "hashed_files": app.state.plan_status["hashed_files"],
                    "total_files": app.state.plan_status["total_files"],
                    "started_at": app.state.plan_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                }
            finally:
                plan_lock.release()

        threading.Thread(target=worker, name="archiver-plan", daemon=True).start()
        return True

    def start_stage_job(trigger: str, disc_code: str) -> bool:
        if not stage_lock.acquire(blocking=False):
            return False

        def worker() -> None:
            app.state.stage_status = {
                "state": "running",
                "disc_code": disc_code,
                "copied_files": 0,
                "total_files": 0,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Staging started by {trigger}.",
            }
            try:
                result = stage_disc(conn, settings, disc_code, progress_callback=stage_progress_callback)
                app.state.stage_status = {
                    "state": "completed",
                    "disc_code": result.disc_code,
                    "copied_files": result.file_count,
                    "total_files": result.file_count,
                    "started_at": app.state.stage_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Staged {result.disc_code}: {result.file_count} files.",
                }
            except Exception as exc:
                logger.exception("background stage failed")
                app.state.stage_status = {
                    "state": "failed",
                    "disc_code": disc_code,
                    "copied_files": app.state.stage_status["copied_files"],
                    "total_files": app.state.stage_status["total_files"],
                    "started_at": app.state.stage_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                }
            finally:
                stage_lock.release()

        threading.Thread(target=worker, name="archiver-stage", daemon=True).start()
        return True

    def start_prepare_job(trigger: str) -> bool:
        if not prepare_lock.acquire(blocking=False):
            return False

        def worker() -> None:
            app.state.prepare_status = {
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": f"Prepare workflow started by {trigger}.",
            }
            try:
                disc = active_disc(conn)
                if disc is None:
                    scan_result = run_scan_cycle(conn, settings, plan_progress_callback=plan_progress_callback)
                    app.state.prepare_status["message"] = scan_result.message
                    disc = active_disc(conn)

                if disc is None:
                    app.state.prepare_status = {
                        "state": "completed",
                        "started_at": app.state.prepare_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": "No disc ready after scan/plan. Threshold may not be reached yet.",
                    }
                    return

                disc_code = disc["disc_code"]
                disc_status = disc["status"]
                if disc_status == "planned":
                    approve_disc(conn, disc_code)
                    disc_status = "approved"
                if disc_status == "approved":
                    stage_result = stage_disc(conn, settings, disc_code, progress_callback=stage_progress_callback)
                    app.state.stage_status = {
                        "state": "completed",
                        "disc_code": stage_result.disc_code,
                        "copied_files": stage_result.file_count,
                        "total_files": stage_result.file_count,
                        "started_at": app.state.stage_status["started_at"],
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": f"Staged {stage_result.disc_code}: {stage_result.file_count} files.",
                    }
                    disc_status = "staged"

                app.state.prepare_status = {
                    "state": "completed",
                    "started_at": app.state.prepare_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Workflow complete. {disc_code} is now {disc_status}.",
                }
            except Exception as exc:
                logger.exception("combined workflow failed")
                app.state.prepare_status = {
                    "state": "failed",
                    "started_at": app.state.prepare_status["started_at"],
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                }
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
            },
        )

    @app.post("/plan")
    def plan():
        start_plan_job("web")
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
