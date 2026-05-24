from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

try:
    import blake3  # type: ignore
except ImportError:  # pragma: no cover
    blake3 = None


def hash_file(path: Path) -> str:
    hasher, algorithm = _new_hasher()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"{algorithm}:{hasher.hexdigest()}"


def hash_stream(handle: BinaryIO) -> str:
    hasher, algorithm = _new_hasher()

    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        hasher.update(chunk)
    return f"{algorithm}:{hasher.hexdigest()}"


def _new_hasher():
    if blake3 is not None:
        hasher = blake3.blake3()
        algorithm = "blake3"
    else:
        hasher = hashlib.sha256()
        algorithm = "sha256"
    return hasher, algorithm
