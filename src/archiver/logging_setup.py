from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import sys

from .config import Settings


def configure_logging(settings: Settings) -> None:
    root_logger = logging.getLogger()
    if getattr(root_logger, "_archiver_configured", False):
        return

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.log_dir / "archiver.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root_logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    root_logger._archiver_configured = True  # type: ignore[attr-defined]
