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
from .repository import status_summary
from .scanner import root_is_available
from .workflow import run_scan_cycle

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
logger = logging.getLogger(__name__)

def create_app(conn: sqlite3.Connection, settings: Settings, startup_scan: bool = False) -> FastAPI:
    app = FastAPI(title="Archiver")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    scan_lock = threading.Lock()
    plan_lock = threading.Lock()
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
        stage_disc(conn, settings, disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/burn")
    def burn(disc_code: str = Form(...)):
        burn_disc(conn, settings, disc_code)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/scan")
    def scan():
        start_scan_job("web")
        return RedirectResponse(url="/", status_code=303)

    return app
