from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .burner import burn_disc, stage_disc
from .config import Settings
from .planner import approve_disc, plan_disc
from .repository import status_summary
from .scanner import root_is_available

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(conn: sqlite3.Connection, settings: Settings) -> FastAPI:
    app = FastAPI(title="Archiver")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
            },
        )

    @app.post("/plan")
    def plan():
        plan_disc(conn, settings)
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

    return app
