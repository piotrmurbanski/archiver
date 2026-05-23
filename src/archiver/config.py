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
    log_dir: Path
    disc_size_bytes: int
    normal_disc_size_bytes: int
    test_disc_size_bytes: int | None
    fill_ratio: float
    web_host: str
    web_port: int
    timezone: str
    scan_hour: int
    optical_device: str | None
    verify_mount: Path
    auto_plan: bool
    notify_command: str | None
    auto_verify: bool
    verify_retry_count: int
    verify_retry_delay_seconds: int
    verify_mount_wait_seconds: int
    log_level: str
    log_max_bytes: int
    log_backup_count: int

    @property
    def planning_limit_bytes(self) -> int:
        return int(self.disc_size_bytes * self.fill_ratio)

    @property
    def test_mode(self) -> bool:
        return self.test_disc_size_bytes is not None

    @property
    def effective_disc_size_gib(self) -> float:
        return self.disc_size_bytes / 1024**3


def load_settings() -> Settings:
    _load_dotenv()
    roots_env = os.environ.get("ARCHIVER_ROOTS", "/mnt/NASz")
    roots = [Path(item).expanduser() for item in roots_env.split(",") if item.strip()]
    db_path = Path(os.environ.get("ARCHIVER_DB_PATH", "./data/archive.db")).expanduser()
    manifests_dir = Path(os.environ.get("ARCHIVER_MANIFESTS_DIR", "./manifests")).expanduser()
    staging_dir = Path(os.environ.get("ARCHIVER_STAGING_DIR", "./staging")).expanduser()
    iso_dir = Path(os.environ.get("ARCHIVER_ISO_DIR", "./iso")).expanduser()
    log_dir = Path(os.environ.get("ARCHIVER_LOG_DIR", "./logs")).expanduser()
    disc_size_gb = int(os.environ.get("ARCHIVER_DISC_SIZE_GB", "100"))
    test_disc_size_gb_env = os.environ.get("ARCHIVER_TEST_DISC_SIZE_GB")
    test_disc_size_bytes = int(test_disc_size_gb_env) * 1024**3 if test_disc_size_gb_env else None
    fill_ratio = float(os.environ.get("ARCHIVER_FILL_RATIO", "0.93"))
    web_host = os.environ.get("ARCHIVER_WEB_HOST", "127.0.0.1")
    web_port = int(os.environ.get("ARCHIVER_WEB_PORT", "8765"))
    timezone = os.environ.get("ARCHIVER_TIMEZONE", "Europe/Warsaw")
    scan_hour = int(os.environ.get("ARCHIVER_SCAN_HOUR", "10"))
    optical_device = os.environ.get("ARCHIVER_OPTICAL_DEVICE") or None
    verify_mount = Path(os.environ.get("ARCHIVER_VERIFY_MOUNT", "/home/piotr/sandbox/archiver/mnt/archiver-disc")).expanduser()
    auto_plan = os.environ.get("ARCHIVER_AUTO_PLAN", "true").strip().lower() in {"1", "true", "yes", "on"}
    notify_command = os.environ.get("ARCHIVER_NOTIFY_COMMAND") or None
    auto_verify = os.environ.get("ARCHIVER_AUTO_VERIFY", "false").strip().lower() in {"1", "true", "yes", "on"}
    verify_retry_count = int(os.environ.get("ARCHIVER_VERIFY_RETRY_COUNT", "10"))
    verify_retry_delay_seconds = int(os.environ.get("ARCHIVER_VERIFY_RETRY_DELAY_SECONDS", "6"))
    verify_mount_wait_seconds = int(os.environ.get("ARCHIVER_VERIFY_MOUNT_WAIT_SECONDS", "20"))
    log_level = os.environ.get("ARCHIVER_LOG_LEVEL", "INFO").strip().upper()
    log_max_bytes = int(os.environ.get("ARCHIVER_LOG_MAX_BYTES", "10485760"))
    log_backup_count = int(os.environ.get("ARCHIVER_LOG_BACKUP_COUNT", "5"))
    return Settings(
        db_path=db_path,
        roots=roots,
        manifests_dir=manifests_dir,
        staging_dir=staging_dir,
        iso_dir=iso_dir,
        log_dir=log_dir,
        disc_size_bytes=test_disc_size_bytes or (disc_size_gb * 1024**3),
        normal_disc_size_bytes=disc_size_gb * 1024**3,
        test_disc_size_bytes=test_disc_size_bytes,
        fill_ratio=fill_ratio,
        web_host=web_host,
        web_port=web_port,
        timezone=timezone,
        scan_hour=scan_hour,
        optical_device=optical_device,
        verify_mount=verify_mount,
        auto_plan=auto_plan,
        notify_command=notify_command,
        auto_verify=auto_verify,
        verify_retry_count=verify_retry_count,
        verify_retry_delay_seconds=verify_retry_delay_seconds,
        verify_mount_wait_seconds=verify_mount_wait_seconds,
        log_level=log_level,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )
