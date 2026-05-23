from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .db import transaction
from .hashing import hash_file

logger = logging.getLogger(__name__)
StageProgressCallback = Callable[[str, int, int], None]
BurnProgressCallback = Callable[[str, str, float | None], None]
_MEDIA_BLOCKS_PATTERN = re.compile(
    r"Media blocks\s*:\s*\d+\s+readable\s*,\s*(\d+)\s+writable\s*,\s*(\d+)\s+overall",
    re.IGNORECASE,
)
_GROWISOFS_FATAL_MARKERS = (
    "unable to write@lba",
    "write failed",
    "input/output error",
    "flush cache failed",
    "reset occurred",
)


@dataclass(slots=True)
class StageResult:
    disc_code: str
    stage_dir: Path
    file_count: int


@dataclass(slots=True)
class BurnResult:
    disc_code: str
    iso_path: Path
    verified: bool
    verify_mount: Path | None
    verify_error: str | None


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


def _count_staged_payload_files(stage_dir: Path) -> int:
    count = 0
    for path in stage_dir.rglob("*"):
        if not path.is_file():
            continue
        if "index" in path.parts:
            continue
        count += 1
    return count


def _ensure_staging_space(settings: Settings) -> None:
    target_dir = settings.staging_dir.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target_dir)
    required_bytes = settings.disc_size_bytes
    if usage.free < required_bytes:
        logger.error(
            "not enough free space for staging: required=%s available=%s path=%s",
            _format_bytes(required_bytes),
            _format_bytes(usage.free),
            target_dir,
        )
        raise RuntimeError(
            "Not enough free space for staging: "
            f"required at least {_format_bytes(required_bytes)}, "
            f"available {_format_bytes(usage.free)} in {target_dir}"
        )


def stage_disc(
    conn: sqlite3.Connection,
    settings: Settings,
    disc_code: str,
    progress_callback: StageProgressCallback | None = None,
) -> StageResult:
    disc = _disc_row(conn, disc_code)
    files = _disc_files(conn, disc["id"])
    _ensure_staging_space(settings)
    logger.info("staging disc %s with %d files", disc_code, len(files))
    stage_dir = settings.staging_dir / disc_code
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    total_files = len(files)
    for index, row in enumerate(files, start=1):
        _copy_file(Path(row["absolute_path"]), stage_dir / row["relative_path_on_disc"])
        if progress_callback is not None:
            progress_callback(disc_code, index, total_files)

    index_dir = stage_dir / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    _copy_file(settings.manifests_dir / f"{disc_code}.csv", index_dir / f"{disc_code}.csv")
    _copy_file(settings.manifests_dir / f"{disc_code}.json", index_dir / f"{disc_code}.json")

    staged_file_count = _count_staged_payload_files(stage_dir)
    if staged_file_count != len(files):
        logger.error(
            "staging file count mismatch for %s: expected=%d actual=%d",
            disc_code,
            len(files),
            staged_file_count,
        )
        raise RuntimeError(
            f"Staging verification failed for {disc_code}: expected {len(files)} files, "
            f"found {staged_file_count} copied files"
        )

    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'staged', updated_at = ? WHERE id = ?", (now, disc["id"]))
        conn.execute(
            "UPDATE files SET status = 'staged' WHERE disc_id = ? AND status IN ('planned', 'approved', 'staged')",
            (disc["id"],),
        )
    logger.info(
        "staging completed for %s at %s (%d/%d files verified)",
        disc_code,
        stage_dir,
        staged_file_count,
        len(files),
    )
    return StageResult(disc_code=disc_code, stage_dir=stage_dir, file_count=len(files))


def _require_xorriso() -> str:
    binary = shutil.which("xorriso")
    if binary is None:
        raise RuntimeError("xorriso is required for burn/verify workflow")
    return binary


def _require_growisofs() -> str:
    binary = shutil.which("growisofs")
    if binary is None:
        raise RuntimeError("growisofs is required for optical burn workflow")
    return binary


def _probe_optical_writable_bytes(settings: Settings) -> int:
    if not settings.optical_device:
        raise RuntimeError("ARCHIVER_OPTICAL_DEVICE is not configured")
    xorriso = _require_xorriso()
    result = subprocess.run(
        [xorriso, "-outdev", settings.optical_device, "-toc"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Unable to probe optical media capacity: {output or result.returncode}")
    match = _MEDIA_BLOCKS_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"Unable to parse optical media capacity from xorriso output: {output}")
    writable_blocks = int(match.group(1))
    writable_bytes = writable_blocks * 2048
    logger.info(
        "optical media capacity probe: device=%s writable_blocks=%d writable_bytes=%s",
        settings.optical_device,
        writable_blocks,
        _format_bytes(writable_bytes),
    )
    return writable_bytes


def _ensure_iso_fits_optical_media(settings: Settings, disc_code: str, iso_path: Path) -> None:
    iso_size = iso_path.stat().st_size
    writable_bytes = _probe_optical_writable_bytes(settings)
    if iso_size > writable_bytes:
        logger.error(
            "iso too large for optical media: disc=%s iso=%s media=%s path=%s",
            disc_code,
            _format_bytes(iso_size),
            _format_bytes(writable_bytes),
            iso_path,
        )
        raise RuntimeError(
            f"ISO for {disc_code} does not fit on the inserted disc: "
            f"image size {_format_bytes(iso_size)}, media capacity {_format_bytes(writable_bytes)}"
        )
    logger.info(
        "iso fits optical media: disc=%s iso=%s media=%s",
        disc_code,
        _format_bytes(iso_size),
        _format_bytes(writable_bytes),
    )


def _log_subprocess_output(prefix: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        logger.info("%s stdout:\n%s", prefix, result.stdout.strip())
    if result.stderr:
        logger.info("%s stderr:\n%s", prefix, result.stderr.strip())


def _growisofs_output_has_failure(output_lines: list[str]) -> str | None:
    for line in output_lines:
        lowered = line.lower()
        for marker in _GROWISOFS_FATAL_MARKERS:
            if marker in lowered:
                return line
    return None


def _log_optical_diagnostics(settings: Settings, context: str) -> None:
    if not settings.optical_device:
        return
    xorriso = shutil.which("xorriso")
    if xorriso is not None:
        result = subprocess.run(
            [xorriso, "-outdev", settings.optical_device, "-toc"],
            check=False,
            capture_output=True,
            text=True,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if output:
            logger.info("optical diagnostics (%s) xorriso:\n%s", context, output)
    mediainfo = shutil.which("dvd+rw-mediainfo")
    if mediainfo is not None:
        result = subprocess.run(
            [mediainfo, settings.optical_device],
            check=False,
            capture_output=True,
            text=True,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if output:
            logger.info("optical diagnostics (%s) dvd+rw-mediainfo:\n%s", context, output)


_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)%\s+done", re.IGNORECASE)


def _report_burn_progress(
    progress_callback: BurnProgressCallback | None,
    disc_code: str,
    message: str,
) -> None:
    if progress_callback is None:
        return
    match = _PERCENT_PATTERN.search(message)
    progress_percent = float(match.group(1)) if match else None
    progress_callback(disc_code, message, progress_percent)


def _mounted_device_path(device: str) -> Path | None:
    lsblk = shutil.which("lsblk")
    if lsblk is None:
        return None
    result = subprocess.run(
        [lsblk, "-n", "-o", "MOUNTPOINT", device],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        mountpoint = line.strip()
        if mountpoint:
            path = Path(mountpoint)
            if path.exists():
                return path
    return None


def _mount_optical_disc(settings: Settings) -> Path:
    if not settings.optical_device:
        raise RuntimeError("ARCHIVER_OPTICAL_DEVICE is not configured")

    fallback_error: str | None = None
    udisksctl = shutil.which("udisksctl")
    if udisksctl is not None:
        logger.info("attempting optical mount via udisksctl for %s", settings.optical_device)
        result = subprocess.run(
            [udisksctl, "mount", "-b", settings.optical_device, "--no-user-interaction"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            text = (result.stdout or "") + (result.stderr or "")
            marker = " at "
            if marker in text:
                mount_candidate = text.split(marker, 1)[1].strip().rstrip(".")
                mount_path = Path(mount_candidate)
                if mount_path.exists():
                    logger.info("disc mounted at %s", mount_path)
                    return mount_path
            for _ in range(5):
                mounted_path = _mounted_device_path(settings.optical_device)
                if mounted_path is not None:
                    logger.info("disc already mounted at %s", mounted_path)
                    return mounted_path
                time.sleep(1)
            raise RuntimeError(f"udisksctl reported success but mount path was not detected: {text.strip()}")
        else:
            error_text = ((result.stdout or "") + (result.stderr or "")).strip()
            if error_text and "already mounted" in error_text.lower():
                mounted_path = _mounted_device_path(settings.optical_device)
                if mounted_path is not None:
                    logger.info("disc already mounted at %s", mounted_path)
                    return mounted_path
            elif error_text:
                fallback_error = error_text

    mount_binary = shutil.which("mount")
    if mount_binary is None:
        if fallback_error:
            raise RuntimeError(f"Unable to auto-mount disc: {fallback_error}")
        raise RuntimeError("Unable to auto-mount disc: no supported mount tool found")

    settings.verify_mount.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [mount_binary, "-o", "ro", settings.optical_device, str(settings.verify_mount)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error_text = ((result.stdout or "") + (result.stderr or "")).strip()
        if fallback_error and error_text:
            raise RuntimeError(f"Unable to auto-mount disc: {fallback_error}; fallback mount failed: {error_text}")
        if fallback_error:
            raise RuntimeError(f"Unable to auto-mount disc: {fallback_error}")
        raise RuntimeError(f"Unable to auto-mount disc: {error_text}")
    return settings.verify_mount


def _unmount_optical_disc(settings: Settings, mount_path: Path) -> None:
    logger.info("unmounting optical disc from %s", mount_path)
    udisksctl = shutil.which("udisksctl")
    if udisksctl is not None and settings.optical_device:
        subprocess.run(
            [udisksctl, "unmount", "-b", settings.optical_device, "--no-user-interaction"],
            check=False,
            capture_output=True,
            text=True,
        )
        return
    umount_binary = shutil.which("umount")
    if umount_binary is not None:
        subprocess.run([umount_binary, str(mount_path)], check=False, capture_output=True, text=True)


def _attempt_auto_verify(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> Path | None:
    last_error: Exception | None = None
    for attempt in range(1, settings.verify_retry_count + 1):
        mount_path: Path | None = None
        try:
            logger.info("auto-verify attempt %d/%d for %s", attempt, settings.verify_retry_count, disc_code)
            mount_path = _mount_optical_disc(settings)
            verify_disc(conn, settings, disc_code, mount_path=mount_path)
            return mount_path
        except Exception as exc:
            last_error = exc
            logger.warning("auto-verify attempt %d failed for %s: %s", attempt, disc_code, exc)
        finally:
            if mount_path is not None:
                _unmount_optical_disc(settings, mount_path)
        if attempt < settings.verify_retry_count:
            time.sleep(settings.verify_retry_delay_seconds)
    if last_error is not None:
        raise RuntimeError(f"Automatic verify failed: {last_error}") from last_error
    return None


def create_iso(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> Path:
    _disc_row(conn, disc_code)
    stage_dir = settings.staging_dir / disc_code
    if not stage_dir.exists():
        raise RuntimeError(f"Stage directory missing: {stage_dir}")
    settings.iso_dir.mkdir(parents=True, exist_ok=True)
    iso_path = settings.iso_dir / f"{disc_code}.iso"
    xorriso = _require_xorriso()
    logger.info("creating iso for %s at %s", disc_code, iso_path)
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


def burn_disc(
    conn: sqlite3.Connection,
    settings: Settings,
    disc_code: str,
    progress_callback: BurnProgressCallback | None = None,
) -> BurnResult:
    disc = _disc_row(conn, disc_code)
    if not settings.optical_device:
        raise RuntimeError("ARCHIVER_OPTICAL_DEVICE is not configured")
    iso_path = create_iso(conn, settings, disc_code)
    _ensure_iso_fits_optical_media(settings, disc_code, iso_path)
    growisofs = _require_growisofs()
    now = datetime.now(UTC).isoformat()
    _log_optical_diagnostics(settings, f"before burn {disc_code}")
    logger.info("burn started for %s using device %s", disc_code, settings.optical_device)
    _report_burn_progress(progress_callback, disc_code, f"Nagrywanie wystartowalo dla {disc_code}.")
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'burning', updated_at = ? WHERE id = ?", (now, disc["id"]))
        conn.execute("UPDATE files SET status = 'burning' WHERE disc_id = ?", (disc["id"],))
    try:
        process = subprocess.Popen(
            [
                growisofs,
                "-dvd-compat",
                "-Z",
                f"{settings.optical_device}={iso_path}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_lines: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            output_lines.append(line)
            logger.info("growisofs burn for %s: %s", disc_code, line)
            _report_burn_progress(progress_callback, disc_code, line)
        return_code = process.wait()
        fatal_line = _growisofs_output_has_failure(output_lines)
        if return_code != 0 or fatal_line is not None:
            raise subprocess.CalledProcessError(
                return_code if return_code != 0 else 1,
                process.args,
                output="\n".join(output_lines),
                stderr="",
            )
    except subprocess.CalledProcessError as exc:
        _log_subprocess_output(f"growisofs burn for {disc_code}", exc)
        _log_optical_diagnostics(settings, f"after failed burn {disc_code}")
        logger.exception("burn failed for %s", disc_code)
        _report_burn_progress(progress_callback, disc_code, f"Nagrywanie nie powiodlo sie: {exc}")
        failed_at = datetime.now(UTC).isoformat()
        with transaction(conn):
            conn.execute("UPDATE discs SET status = 'burn_failed', updated_at = ? WHERE id = ?", (failed_at, disc["id"]))
            conn.execute(
                "UPDATE files SET status = 'staged' WHERE disc_id = ? AND status = 'burning'",
                (disc["id"],),
            )
        error_text = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
        raise RuntimeError(f"growisofs failed for {disc_code}: {error_text or exc}") from exc
    burned_at = datetime.now(UTC).isoformat()
    logger.info("burn completed for %s", disc_code)
    _report_burn_progress(progress_callback, disc_code, f"Nagrywanie zakonczone dla {disc_code}. Start verify.")
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'burned', updated_at = ? WHERE id = ?", (burned_at, disc["id"]))
        conn.execute("UPDATE files SET status = 'burned' WHERE disc_id = ?", (disc["id"],))
    verified = False
    verify_mount = None
    if settings.auto_verify:
        try:
            verify_mount = _attempt_auto_verify(conn, settings, disc_code)
            verified = True
            verify_error = None
            logger.info("automatic verify completed for %s", disc_code)
            _report_burn_progress(progress_callback, disc_code, f"Verify zakonczone dla {disc_code}.")
        except Exception as exc:
            failed_at = datetime.now(UTC).isoformat()
            with transaction(conn):
                conn.execute(
                    "UPDATE discs SET status = 'verify_failed', updated_at = ? WHERE id = ?",
                    (failed_at, disc["id"]),
                )
            verify_error = str(exc)
            logger.warning("automatic verify failed for %s: %s", disc_code, exc)
            _report_burn_progress(progress_callback, disc_code, f"Verify nie powiodlo sie: {exc}")
    else:
        verify_error = None
    return BurnResult(
        disc_code=disc_code,
        iso_path=iso_path,
        verified=verified,
        verify_mount=verify_mount,
        verify_error=verify_error,
    )


def verify_disc(conn: sqlite3.Connection, settings: Settings, disc_code: str, mount_path: Path | None = None) -> VerifyResult:
    disc = _disc_row(conn, disc_code)
    verify_root = mount_path or settings.verify_mount
    logger.info("verify started for %s using mount %s", disc_code, verify_root)
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
        logger.info("removed staging directory after verify: %s", stage_dir)
    logger.info("verify completed for %s (%d files)", disc_code, len(files))
    return VerifyResult(disc_code=disc_code, checked_files=len(files))
