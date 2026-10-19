"""Artifact storage port.

All operations are mandatory; every adapter must pass ``tests/contract/test_storage_contract.py``.

Keys are ``/``-separated relative paths (``models/qwen2.5-0.5b/config.json``). Adapters
validate keys with :func:`validate_key` so that no adapter can be tricked into path
traversal. Content is streamed through binary file objects so multi-gigabyte weights
never have to fit in memory.

Each ``put`` creates a new *version* of the key; adapters store a SHA-256 checksum and
version metadata alongside the content, and ``verify_checksum`` re-hashes the stored
bytes to detect corruption.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import BinaryIO

from omf.core.domain.errors import ValidationFailed
from omf.core.domain.models import StorageArtifact, StorageStatus

_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._+=@-]{1,255}$")
MAX_KEY_LENGTH = 1024


def validate_key(key: str) -> str:
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValidationFailed("artifact key must be 1-1024 characters", details={"key": key})
    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValidationFailed("artifact key must be a relative '/'-separated path")
    for segment in key.split("/"):
        if segment in ("", ".", "..") or not _KEY_SEGMENT.match(segment):
            raise ValidationFailed(
                f"invalid artifact key segment {segment!r}",
                details={"key": key, "allowed": "A-Z a-z 0-9 . _ + = @ -"},
            )
    return key


def validate_prefix(prefix: str) -> str:
    if prefix == "":
        return prefix
    return validate_key(prefix.rstrip("/")) + ("/" if prefix.endswith("/") else "")


class ArtifactStorageProvider(ABC):
    adapter_type: str = "unknown"

    def __init__(self, storage_provider_id: str) -> None:
        self.storage_provider_id = storage_provider_id

    @abstractmethod
    async def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StorageArtifact:
        """Store ``source`` (read to EOF) under ``key``; returns the new version's metadata."""

    @abstractmethod
    async def get(self, key: str, destination: BinaryIO) -> StorageArtifact:
        """Write the latest version of ``key`` into ``destination``."""

    @abstractmethod
    async def list(self, prefix: str = "") -> list[StorageArtifact]:
        """Latest version of every artifact whose key starts with ``prefix``, sorted by key."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete all versions of ``key``. Raises ``ArtifactNotFound`` if absent."""

    @abstractmethod
    async def get_metadata(self, key: str) -> StorageArtifact: ...

    @abstractmethod
    async def verify_checksum(self, key: str, expected_sha256: str | None = None) -> bool:
        """Re-hash stored content and compare with the recorded (or expected) checksum."""

    @abstractmethod
    async def status(self) -> StorageStatus: ...

    async def aclose(self) -> None:
        return None
