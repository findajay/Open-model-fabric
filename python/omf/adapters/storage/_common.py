"""Helpers shared by storage adapters: streaming hashing and spooling."""

from __future__ import annotations

import hashlib
import tempfile
from typing import IO, BinaryIO

CHUNK = 1024 * 1024
MAX_METADATA_ENTRIES = 32
MAX_METADATA_VALUE = 256


def spool_and_hash(source: BinaryIO, directory: str | None = None) -> tuple[IO[bytes], int, str]:
    """Copy ``source`` into a temporary file while hashing. Returns (file, size, sha256).

    The returned file is positioned at 0; the caller must close it.
    """
    digest = hashlib.sha256()
    size = 0
    tmp = tempfile.NamedTemporaryFile(dir=directory, delete=True)  # noqa: SIM115 - returned
    try:
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            tmp.write(chunk)
        tmp.flush()
        tmp.seek(0)
    except BaseException:
        tmp.close()
        raise
    return tmp, size, digest.hexdigest()


def hash_stream(source: BinaryIO, destination: BinaryIO | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        if destination is not None:
            destination.write(chunk)
    return size, digest.hexdigest()


class HashingWriter:
    """File-like sink that hashes what passes through (optionally forwarding it)."""

    def __init__(self, destination: BinaryIO | None = None) -> None:
        self._dest = destination
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        self.size += len(data)
        if self._dest is not None:
            self._dest.write(data)
        return len(data)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def validate_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    from omf.core.domain.errors import ValidationFailed

    metadata = metadata or {}
    if len(metadata) > MAX_METADATA_ENTRIES:
        raise ValidationFailed(f"at most {MAX_METADATA_ENTRIES} metadata entries are allowed")
    for k, v in metadata.items():
        if not k.replace("_", "").replace("-", "").isalnum() or len(k) > 64:
            raise ValidationFailed(f"invalid metadata key {k!r}")
        if not isinstance(v, str) or len(v) > MAX_METADATA_VALUE:
            raise ValidationFailed(f"metadata value for {k!r} must be a string <= 256 chars")
    return dict(metadata)
