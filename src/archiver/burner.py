from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import Settings
from .db import transaction
from .hashing import hash_file, hash_stream

logger = logging.getLogger(__name__)
StageProgressCallback = Callable[[str, int, int], None]
BurnProgressCallback = Callable[[str, str, float | None], None]
VerifyProgressCallback = Callable[[str, int, int], None]
_VERIFY_COMPARE_CHUNK_SIZE = 8 * 1024 * 1024
_MEDIA_BLOCKS_PATTERN = re.compile(
    r"Media blocks\s*:\s*\d+\s+readable\s*,\s*(\d+)\s+writable\s*,\s*(\d+)\s+overall",
    re.IGNORECASE,
)
_MEDIA_CURRENT_PATTERN = re.compile(r"Media current:\s*(.+)", re.IGNORECASE)
_MEDIA_STATUS_PATTERN = re.compile(r"Media status\s*:\s*(.+)", re.IGNORECASE)
_MEDIA_SUMMARY_PATTERN = re.compile(r"Media summary:\s*(.+)", re.IGNORECASE)
_GROWISOFS_FATAL_MARKERS = (
    "unable to write@lba",
    "write failed",
    "input/output error",
    "flush cache failed",
    "reset occurred",
)
_BURN_READY_RETRIES = 6
_BURN_READY_DELAY_SECONDS = 5
_BURN_READY_STABLE_POLLS = 2


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


@dataclass(slots=True)
class OpticalMediaProbe:
    writable_bytes: int
    writable_blocks: int
    media_current: str | None
    media_status: str | None
    media_summary: str | None


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
    if disc["status"] not in {"planned", "approved", "staged"}:
        raise RuntimeError(
            f"Disc {disc_code} cannot be staged from status {disc['status']}. "
            "Only planned, approved, and staged discs can be staged."
        )
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
        conn.execute(
            """
            UPDATE discs
            SET status = 'staged',
                approved_at = COALESCE(approved_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, disc["id"]),
        )
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


def probe_optical_media(settings: Settings) -> OpticalMediaProbe:
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
    current_match = _MEDIA_CURRENT_PATTERN.search(output)
    status_match = _MEDIA_STATUS_PATTERN.search(output)
    summary_match = _MEDIA_SUMMARY_PATTERN.search(output)
    logger.info(
        "optical media capacity probe: device=%s writable_blocks=%d writable_bytes=%s",
        settings.optical_device,
        writable_blocks,
        _format_bytes(writable_bytes),
    )
    return OpticalMediaProbe(
        writable_bytes=writable_bytes,
        writable_blocks=writable_blocks,
        media_current=current_match.group(1).strip() if current_match else None,
        media_status=status_match.group(1).strip() if status_match else None,
        media_summary=summary_match.group(1).strip() if summary_match else None,
    )


def _ensure_iso_fits_optical_media(settings: Settings, disc_code: str, iso_path: Path) -> None:
    iso_size = iso_path.stat().st_size
    writable_bytes = probe_optical_media(settings).writable_bytes
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


_PERCENT_DONE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)%\s+done", re.IGNORECASE)
_PERCENT_INLINE_PATTERN = re.compile(r"\(\s*(\d+(?:\.\d+)?)%\)")


def _extract_burn_progress_percent(message: str) -> float | None:
    for pattern in (_PERCENT_DONE_PATTERN, _PERCENT_INLINE_PATTERN):
        match = pattern.search(message)
        if match:
            return float(match.group(1))
    return None


def _clean_burn_output_line(line: str) -> str:
    cleaned = line.strip()
    for prefix in (":-[", ":-(", ":"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip(" ]")
    return cleaned.strip()


def _summarize_burn_progress_line(disc_code: str, message: str) -> str:
    progress_percent = _extract_burn_progress_percent(message)
    if progress_percent is not None:
        return f"Nagrywanie {disc_code}: {progress_percent:.1f}%"

    lowered = message.lower()
    if "pre-formatting blank bd-r" in lowered:
        return f"Przygotowuję nośnik {disc_code} do nagrywania."
    if "current write speed" in lowered:
        return f"Napęd potwierdził prędkość zapisu dla {disc_code}."
    if "flushing cache" in lowered:
        return f"Kończę zapis i opróżniam cache dla {disc_code}."
    if "closing disc" in lowered:
        return f"Zamykam płytę {disc_code}."
    if "reloading tray" in lowered:
        return f"Napęd przeładowuje płytę {disc_code}."
    return _clean_burn_output_line(message)


def _summarize_growisofs_failure(disc_code: str, output_lines: list[str]) -> str:
    progress_percent: float | None = None
    interesting_lines: list[str] = []
    for line in output_lines:
        maybe_percent = _extract_burn_progress_percent(line)
        if maybe_percent is not None:
            progress_percent = maybe_percent
        lowered = line.lower()
        if any(marker in lowered for marker in _GROWISOFS_FATAL_MARKERS):
            cleaned = _clean_burn_output_line(line)
            if cleaned and cleaned not in interesting_lines:
                interesting_lines.append(cleaned)

    reason = interesting_lines[0] if interesting_lines else "Nieznany błąd nagrywania"
    if len(interesting_lines) > 1:
        reason = f"{reason}; {interesting_lines[1]}"
    if progress_percent is not None:
        return (
            f"Nagrywanie {disc_code} nie powiodło się przy {progress_percent:.1f}%: "
            f"{reason}. Szczegóły są w logu."
        )
    return f"Nagrywanie {disc_code} nie powiodło się: {reason}. Szczegóły są w logu."


def _report_burn_progress(
    progress_callback: BurnProgressCallback | None,
    disc_code: str,
    message: str,
) -> None:
    if progress_callback is None:
        return
    progress_percent = _extract_burn_progress_percent(message)
    progress_callback(
        disc_code,
        _summarize_burn_progress_line(disc_code, message),
        progress_percent,
    )


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


def _wait_for_optical_ready_for_burn(
    settings: Settings,
    disc_code: str,
    progress_callback: BurnProgressCallback | None = None,
) -> OpticalMediaProbe:
    if not settings.optical_device:
        raise RuntimeError("ARCHIVER_OPTICAL_DEVICE is not configured")

    mounted_path = _mounted_device_path(settings.optical_device)
    if mounted_path is not None:
        logger.info("optical device %s is mounted at %s, unmounting before burn", settings.optical_device, mounted_path)
        _report_burn_progress(
            progress_callback,
            disc_code,
            f"Napęd był zamontowany w {mounted_path}. Odpinam go przed nagrywaniem.",
        )
        _unmount_optical_disc(settings, mounted_path)
        time.sleep(2)

    last_signature: tuple[int, str | None, str | None] | None = None
    stable_polls = 0
    last_probe: OpticalMediaProbe | None = None

    for attempt in range(1, _BURN_READY_RETRIES + 1):
        try:
            probe = probe_optical_media(settings)
        except Exception as exc:
            logger.warning("optical ready probe %d/%d failed for %s: %s", attempt, _BURN_READY_RETRIES, disc_code, exc)
            _report_burn_progress(
                progress_callback,
                disc_code,
                f"Czekam az napęd będzie gotowy do nagrywania ({attempt}/{_BURN_READY_RETRIES}).",
            )
            time.sleep(_BURN_READY_DELAY_SECONDS)
            continue

        last_probe = probe
        signature = (probe.writable_blocks, probe.media_current, probe.media_status)
        is_writable = probe.writable_blocks > 0
        if signature == last_signature and is_writable:
            stable_polls += 1
        else:
            stable_polls = 1 if is_writable else 0
            last_signature = signature

        logger.info(
            "optical ready probe %d/%d for %s: writable=%d status=%s current=%s stable=%d/%d",
            attempt,
            _BURN_READY_RETRIES,
            disc_code,
            probe.writable_blocks,
            probe.media_status,
            probe.media_current,
            stable_polls,
            _BURN_READY_STABLE_POLLS,
        )
        _report_burn_progress(
            progress_callback,
            disc_code,
            (
                f"Sprawdzam gotowość napędu ({attempt}/{_BURN_READY_RETRIES}): "
                f"{probe.media_current or '-'}, {probe.media_status or '-'}."
            ),
        )
        if stable_polls >= _BURN_READY_STABLE_POLLS:
            logger.info("optical device ready for burn %s after %d probes", disc_code, attempt)
            time.sleep(2)
            return probe
        time.sleep(_BURN_READY_DELAY_SECONDS)

    if last_probe is not None:
        raise RuntimeError(
            "Napęd nie osiągnął stabilnego stanu gotowości do nagrywania. "
            f"Ostatni stan: {last_probe.media_current or '-'}, {last_probe.media_status or '-'}."
        )
    raise RuntimeError("Napęd nie odpowiedział poprawnie podczas przygotowania do nagrywania.")


def _extract_iso_file_to_path(optical_device: str, iso_rr_path: str, dest_path: Path) -> None:
    xorriso = _require_xorriso()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            xorriso,
            "-osirrox",
            "on",
            "-indev",
            optical_device,
            "-extract",
            iso_rr_path,
            str(dest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error_text = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        raise RuntimeError(f"Unable to read {iso_rr_path} from disc: {error_text or result.returncode}")


def _extract_iso_file_hash_from_device(optical_device: str, iso_rr_path: str) -> str:
    xorriso = _require_xorriso()
    with TemporaryDirectory(prefix="archiver-verify-stream-") as temp_root_str:
        temp_root = Path(temp_root_str)
        fifo_path = temp_root / "stream.fifo"
        os.mkfifo(fifo_path)
        process = subprocess.Popen(
            [
                xorriso,
                "-osirrox",
                "on",
                "-indev",
                optical_device,
                "-extract",
                iso_rr_path,
                str(fifo_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with fifo_path.open("rb", buffering=0) as handle:
                actual_hash = hash_stream(handle)
            stdout, stderr = process.communicate()
        except Exception:
            process.kill()
            process.wait()
            raise
        if process.returncode != 0:
            error_text = ((stdout or "") + "\n" + (stderr or "")).strip()
            raise RuntimeError(f"Unable to stream-read {iso_rr_path} from disc: {error_text or process.returncode}")
        return actual_hash


def _compare_iso_with_device(
    iso_path: Path,
    device_path: str,
    progress_callback: VerifyProgressCallback | None = None,
    disc_code: str | None = None,
) -> None:
    expected_size = iso_path.stat().st_size
    if expected_size == 0:
        raise RuntimeError(f"ISO image is empty: {iso_path}")

    compared_bytes = 0
    total_chunks = max(1, (expected_size + _VERIFY_COMPARE_CHUNK_SIZE - 1) // _VERIFY_COMPARE_CHUNK_SIZE)
    compared_chunks = 0
    last_reported_chunk = 0

    with iso_path.open("rb") as iso_handle, Path(device_path).open("rb") as device_handle:
        while compared_bytes < expected_size:
            bytes_left = expected_size - compared_bytes
            chunk_size = min(_VERIFY_COMPARE_CHUNK_SIZE, bytes_left)
            iso_chunk = iso_handle.read(chunk_size)
            device_chunk = device_handle.read(chunk_size)
            if len(iso_chunk) != chunk_size:
                raise RuntimeError(f"Unexpected end of ISO image while verifying: {iso_path}")
            if len(device_chunk) != chunk_size:
                raise RuntimeError(
                    "Unexpected end of optical device while verifying. "
                    f"Expected {expected_size} bytes from {device_path}."
                )
            if iso_chunk != device_chunk:
                raise RuntimeError(
                    "Disc contents differ from the generated ISO image. "
                    f"First mismatch after {compared_bytes} bytes."
                )
            compared_bytes += chunk_size
            compared_chunks += 1
            if (
                progress_callback is not None
                and disc_code is not None
                and (compared_chunks == total_chunks or compared_chunks - last_reported_chunk >= 8)
            ):
                progress_callback(disc_code, compared_chunks, total_chunks)
                last_reported_chunk = compared_chunks


def _mark_disc_verified(conn: sqlite3.Connection, settings: Settings, disc_id: int, disc_code: str) -> None:
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'verified', updated_at = ? WHERE id = ?", (now, disc_id))
        conn.execute(
            "UPDATE files SET status = 'verified', archived_at = ?, changed_after_archive = 0 WHERE disc_id = ?",
            (now, disc_id),
        )
    stage_dir = settings.staging_dir / disc_code
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
        logger.info("removed staging directory after verify: %s", stage_dir)


def _verify_disc_from_device(
    conn: sqlite3.Connection,
    settings: Settings,
    disc_code: str,
    progress_callback: VerifyProgressCallback | None = None,
    speed_mode: bool = False,
) -> VerifyResult:
    if not settings.optical_device:
        raise RuntimeError("ARCHIVER_OPTICAL_DEVICE is not configured")

    disc = _disc_row(conn, disc_code)
    files = _disc_files(conn, disc["id"])
    logger.info("verify from device started for %s using %s", disc_code, settings.optical_device)

    if speed_mode:
        logger.info("using streaming file-level verify for %s without temporary files", disc_code)
        for index, row in enumerate(files, start=1):
            iso_rr_path = "/" + row["relative_path_on_disc"].replace(os.sep, "/").lstrip("/")
            actual_hash = _extract_iso_file_hash_from_device(settings.optical_device, iso_rr_path)
            if actual_hash != row["content_hash"]:
                raise RuntimeError(f"Hash mismatch for {row['relative_path_on_disc']}")
            if progress_callback is not None:
                progress_callback(disc_code, index, len(files))

        for index_name in (f"{disc_code}.csv", f"{disc_code}.json"):
            manifest_path = settings.manifests_dir / index_name
            expected_hash = hash_file(manifest_path)
            actual_hash = _extract_iso_file_hash_from_device(
                settings.optical_device,
                f"/index/{index_name}",
            )
            if actual_hash != expected_hash:
                raise RuntimeError(f"Hash mismatch for index/{index_name}")

        _mark_disc_verified(conn, settings, disc["id"], disc_code)
        logger.info("streaming verify from device completed for %s (%d files)", disc_code, len(files))
        return VerifyResult(disc_code=disc_code, checked_files=len(files))

    iso_path = settings.iso_dir / f"{disc_code}.iso"
    if iso_path.exists():
        logger.info("using fast device-level verify for %s against %s", disc_code, iso_path)
        _compare_iso_with_device(
            iso_path,
            settings.optical_device,
            progress_callback=progress_callback,
            disc_code=disc_code,
        )
        _mark_disc_verified(conn, settings, disc["id"], disc_code)
        logger.info("fast verify from device completed for %s (%d files)", disc_code, len(files))
        return VerifyResult(disc_code=disc_code, checked_files=len(files))

    with TemporaryDirectory(prefix=f"archiver-verify-{disc_code.lower()}-") as temp_root_str:
        temp_root = Path(temp_root_str)

        for index, row in enumerate(files, start=1):
            iso_rr_path = "/" + row["relative_path_on_disc"].replace(os.sep, "/").lstrip("/")
            extracted_path = temp_root / row["relative_path_on_disc"]
            _extract_iso_file_to_path(settings.optical_device, iso_rr_path, extracted_path)
            actual_hash = hash_file(extracted_path)
            if actual_hash != row["content_hash"]:
                raise RuntimeError(f"Hash mismatch for {row['relative_path_on_disc']}")
            if progress_callback is not None:
                progress_callback(disc_code, index, len(files))

        for index_name in (f"{disc_code}.csv", f"{disc_code}.json"):
            _extract_iso_file_to_path(
                settings.optical_device,
                f"/index/{index_name}",
                temp_root / "index" / index_name,
            )

    _mark_disc_verified(conn, settings, disc["id"], disc_code)
    logger.info("verify from device completed for %s (%d files)", disc_code, len(files))
    return VerifyResult(disc_code=disc_code, checked_files=len(files))


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


def mount_and_verify_disc(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> VerifyResult:
    last_error: Exception | None = None
    for attempt in range(settings.verify_mount_wait_seconds + 1):
        try:
            return _verify_disc_from_device(conn, settings, disc_code)
        except Exception as exc:
            last_error = exc
            if attempt < settings.verify_mount_wait_seconds:
                time.sleep(1)
    raise RuntimeError(
        "Nie udalo sie odczytac plyty do verify. "
        f"Ostatni blad: {last_error}"
    ) from last_error


def auto_mount_and_verify_disc(conn: sqlite3.Connection, settings: Settings, disc_code: str) -> VerifyResult:
    mount_path = _mount_optical_disc(settings)
    try:
        return verify_disc(conn, settings, disc_code, mount_path=mount_path)
    finally:
        _unmount_optical_disc(settings, mount_path)


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
    _wait_for_optical_ready_for_burn(settings, disc_code, progress_callback=progress_callback)
    logger.info("burn started for %s using device %s", disc_code, settings.optical_device)
    _report_burn_progress(progress_callback, disc_code, f"Nagrywanie wystartowalo dla {disc_code}.")
    with transaction(conn):
        conn.execute("UPDATE discs SET status = 'burning', updated_at = ? WHERE id = ?", (now, disc["id"]))
        conn.execute("UPDATE files SET status = 'burning' WHERE disc_id = ?", (disc["id"],))
    output_lines: list[str] = []
    try:
        process = subprocess.Popen(
            [
                growisofs,
                "-speed=2",
                "-dvd-compat",
                "-Z",
                f"{settings.optical_device}={iso_path}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
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
        failure_summary = _summarize_growisofs_failure(disc_code, output_lines)
        _report_burn_progress(progress_callback, disc_code, failure_summary)
        failed_at = datetime.now(UTC).isoformat()
        with transaction(conn):
            conn.execute("UPDATE discs SET status = 'burn_failed', updated_at = ? WHERE id = ?", (failed_at, disc["id"]))
            conn.execute(
                "UPDATE files SET status = 'staged' WHERE disc_id = ? AND status = 'burning'",
                (disc["id"],),
            )
        raise RuntimeError(failure_summary) from exc
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
    if mount_path is None and settings.optical_device:
        iso_path = settings.iso_dir / f"{disc_code}.iso"
        if iso_path.exists():
            return _verify_disc_from_device(conn, settings, disc_code)

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

    _mark_disc_verified(conn, settings, disc["id"], disc_code)
    logger.info("verify completed for %s (%d files)", disc_code, len(files))
    return VerifyResult(disc_code=disc_code, checked_files=len(files))


def verify_disc_from_device(
    conn: sqlite3.Connection,
    settings: Settings,
    disc_code: str,
    progress_callback: VerifyProgressCallback | None = None,
) -> VerifyResult:
    return _verify_disc_from_device(conn, settings, disc_code, progress_callback=progress_callback)


def speed_verify_disc_from_device(
    conn: sqlite3.Connection,
    settings: Settings,
    disc_code: str,
    progress_callback: VerifyProgressCallback | None = None,
) -> VerifyResult:
    return _verify_disc_from_device(
        conn,
        settings,
        disc_code,
        progress_callback=progress_callback,
        speed_mode=True,
    )
