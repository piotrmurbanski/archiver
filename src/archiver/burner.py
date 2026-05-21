from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .db import transaction
from .hashing import hash_file


@dataclass(slots=True)
class StageResult:
    disc_code: str
    stage_dir: Path
    file_count: int


@dataclass(slots=True)
class BurnResult:
    disc_code: str
    iso_path: Path


@dataclass(slots=True)
class VerifyResult:
    disc_code: str
    checked_files: int


def _format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def _disc_row(conn: sqlite3.Connection, disc_code: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, disc_code, label, status
        FROM discs
        WHERE disc_code = ?
        """,
        (disc_code,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Disc not found: {disc_code}")
    return row


def _disc_files(conn: sqlite3.Connection, disc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            f.id,
            f.absolute_path,
            df.relative_path_on_disc,
            df.content_hash,
            df.size_bytes
        FROM disc_files df
        JOIN files f ON f.id = df.file_id
        WHERE df.disc_id = ?
        ORDER BY df.relative_path_on_disc
        """,
        (disc_id,),
    ).fetchall()


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _ensure_staging_space(settings: Settings) -> None:
    target_dir = settings.staging_dir.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target_dir)
    required_bytes = settings.disc_size_bytes
    if usage.free < required_bytes:
        raise RuntimeError(
            "Not enough free space for staging: "
            f"required at least {_format_bytes(required_bytes)}, "
            f"available {_format_bytes(usage.free)} in {target_dir}"
        )


def stage_disc(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> StageResult:
    disc = _disc_row(conn, disc_code)
    files = _disc_files(conn, disc["id"])
    _ensure_staging_space(settings)
    stage_dir = settings.staging_dir / disc_code
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    for row in files:
        _copy_file(Path(row["absolute_path"]), stage_dir / row["relative_path_on_disc"])

    index_dir = stage_dir / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    _copy_file(settings.manifests_dir / f"{disc_code}.csv", index_dir / f"{disc_code}.csv")
    _copy_file(settings.manifests_dir / f"{disc_code}.json", index_dir / f"{disc_code}.json")

    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'staged', updated_at = ? WHERE id = ?", (now, disc["id"]))
        conn.execute(
            "UPDATE files SET status = 'staged' WHERE disc_id = ? AND status IN ('planned', 'approved', 'staged')",
            (disc["id"],),
        )
    return StageResult(disc_code=disc_code, stage_dir=stage_dir, file_count=len(files))


def _require_xorriso() -> str:
    binary = shutil.which("xorriso")
    if binary is None:
        raise RuntimeError("xorriso is required for burn/verify workflow")
    return binary


def create_iso(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> Path:
    _disc_row(conn, disc_code)
    stage_dir = settings.staging_dir / disc_code
    if not stage_dir.exists():
        raise RuntimeError(f"Stage directory missing: {stage_dir}")
    settings.iso_dir.mkdir(parents=True, exist_ok=True)
    iso_path = settings.iso_dir / f"{disc_code}.iso"
    xorriso = _require_xorriso()
    subprocess.run(
        [
            xorriso,
            "-as",
            "mkisofs",
            "-r",
            "-J",
            "-joliet-long",
            "-V",
            disc_code,
            "-o",
            str(iso_path),
            str(stage_dir),
        ],
        check=True,
    )
    return iso_path


def burn_disc(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> BurnResult:
    disc = _disc_row(conn, disc_code)
    if not settings.optical_device:
        raise RuntimeError("ARCHIVER_OPTICAL_DEVICE is not configured")
    iso_path = create_iso(conn, settings, disc_code)
    xorriso = _require_xorriso()
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'burning', updated_at = ? WHERE id = ?", (now, disc["id"]))
        conn.execute("UPDATE files SET status = 'burning' WHERE disc_id = ?", (disc["id"],))
    try:
        subprocess.run(
            [
                xorriso,
                "-as",
                "cdrecord",
                f"dev={settings.optical_device}",
                "-v",
                str(iso_path),
            ],
            check=True,
        )
    except Exception:
        failed_at = datetime.now(UTC).isoformat()
        with transaction(conn):
            conn.execute("UPDATE discs SET status = 'burn_failed', updated_at = ? WHERE id = ?", (failed_at, disc["id"]))
            conn.execute(
                "UPDATE files SET status = 'approved' WHERE disc_id = ? AND status = 'burning'",
                (disc["id"],),
            )
        raise
    burned_at = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'burned', updated_at = ? WHERE id = ?", (burned_at, disc["id"]))
        conn.execute("UPDATE files SET status = 'burned' WHERE disc_id = ?", (disc["id"],))
    return BurnResult(disc_code=disc_code, iso_path=iso_path)


def verify_disc(conn: sqlite3.Connection, settings: Settings, disc_code: str, mount_path: Path | None = None) -> VerifyResult:
    disc = _disc_row(conn, disc_code)
    verify_root = mount_path or settings.verify_mount
    if not verify_root.exists():
        raise RuntimeError(f"Verify mount not found: {verify_root}")
    files = _disc_files(conn, disc["id"])
    for row in files:
        archived_path = verify_root / row["relative_path_on_disc"]
        if not archived_path.exists():
            raise RuntimeError(f"Missing archived file on disc: {archived_path}")
        actual_hash = hash_file(archived_path)
        if actual_hash != row["content_hash"]:
            raise RuntimeError(f"Hash mismatch for {row['relative_path_on_disc']}")

    index_csv = verify_root / "index" / f"{disc_code}.csv"
    index_json = verify_root / "index" / f"{disc_code}.json"
    if not index_csv.exists() or not index_json.exists():
        raise RuntimeError("Index files missing on disc")

    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'verified', updated_at = ? WHERE id = ?", (now, disc["id"]))
        conn.execute(
            "UPDATE files SET status = 'verified', archived_at = ?, changed_after_archive = 0 WHERE disc_id = ?",
            (now, disc["id"]),
        )
    stage_dir = settings.staging_dir / disc_code
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    return VerifyResult(disc_code=disc_code, checked_files=len(files))
