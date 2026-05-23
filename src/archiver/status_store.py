from __future__ import annotations

import json
from pathlib import Path

from .config import Settings


def default_status_payload(settings: Settings) -> dict[str, object]:
    return {
        "scan_status": {
            "state": "idle",
            "started_at": None,
            "finished_at": None,
            "current_root": None,
            "scanned_files": 0,
            "new_files": 0,
            "changed_files": 0,
            "message": "No scan started yet.",
        },
        "plan_status": {
            "state": "idle",
            "disc_code": None,
            "hashed_files": 0,
            "total_files": 0,
            "started_at": None,
            "finished_at": None,
            "message": "No planning started yet.",
        },
        "stage_status": {
            "state": "idle",
            "disc_code": None,
            "copied_files": 0,
            "total_files": 0,
            "started_at": None,
            "finished_at": None,
            "message": "No staging started yet.",
        },
        "prepare_status": {
            "state": "idle",
            "started_at": None,
            "finished_at": None,
            "message": "No combined workflow started yet.",
        },
        "burn_status": {
            "state": "idle",
            "disc_code": None,
            "progress_percent": None,
            "started_at": None,
            "finished_at": None,
            "message": "No burn started yet.",
        },
        "root_checks": [{"root": str(root), "available": None} for root in settings.roots],
    }


def load_status_payload(status_file: Path, settings: Settings) -> dict[str, object]:
    payload = default_status_payload(settings)
    if not status_file.exists():
        return payload
    try:
        raw = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload
    for key, default_value in payload.items():
        if isinstance(default_value, dict):
            payload[key] = {**default_value, **raw.get(key, {})}
        else:
            payload[key] = raw.get(key, default_value)
    return payload


def save_status_payload(status_file: Path, payload: dict[str, object]) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
