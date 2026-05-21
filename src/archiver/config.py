from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(slots=True)
class Settings:
    db_path: Path
    roots: list[Path]
    manifests_dir: Path
    staging_dir: Path
    iso_dir: Path
    disc_size_bytes: int
    fill_ratio: float
    web_host: str
    web_port: int
    timezone: str
    scan_hour: int
    optical_device: str | None
    verify_mount: Path

    @property
    def planning_limit_bytes(self) -> int:
        return int(self.disc_size_bytes * self.fill_ratio)


def load_settings() -> Settings:
    _load_dotenv()
    roots_env = os.environ.get("ARCHIVER_ROOTS", "/mnt/NASz")
    roots = [Path(item).expanduser() for item in roots_env.split(",") if item.strip()]
    db_path = Path(os.environ.get("ARCHIVER_DB_PATH", "./data/archive.db")).expanduser()
    manifests_dir = Path(os.environ.get("ARCHIVER_MANIFESTS_DIR", "./manifests")).expanduser()
    staging_dir = Path(os.environ.get("ARCHIVER_STAGING_DIR", "./staging")).expanduser()
    iso_dir = Path(os.environ.get("ARCHIVER_ISO_DIR", "./iso")).expanduser()
    disc_size_gb = int(os.environ.get("ARCHIVER_DISC_SIZE_GB", "100"))
    fill_ratio = float(os.environ.get("ARCHIVER_FILL_RATIO", "0.93"))
    web_host = os.environ.get("ARCHIVER_WEB_HOST", "127.0.0.1")
    web_port = int(os.environ.get("ARCHIVER_WEB_PORT", "8765"))
    timezone = os.environ.get("ARCHIVER_TIMEZONE", "Europe/Warsaw")
    scan_hour = int(os.environ.get("ARCHIVER_SCAN_HOUR", "10"))
    optical_device = os.environ.get("ARCHIVER_OPTICAL_DEVICE") or None
    verify_mount = Path(os.environ.get("ARCHIVER_VERIFY_MOUNT", "/mnt/archiver-disc")).expanduser()
    return Settings(
        db_path=db_path,
        roots=roots,
        manifests_dir=manifests_dir,
        staging_dir=staging_dir,
        iso_dir=iso_dir,
        disc_size_bytes=disc_size_gb * 1024**3,
        fill_ratio=fill_ratio,
        web_host=web_host,
        web_port=web_port,
        timezone=timezone,
        scan_hour=scan_hour,
        optical_device=optical_device,
        verify_mount=verify_mount,
    )
