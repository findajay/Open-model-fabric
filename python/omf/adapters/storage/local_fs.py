"""Local filesystem artifact storage.

Layout (under ``root``)::

    objects/<h[:2]>/<h>/meta.json        # h = sha256(key); metadata + version history
    objects/<h[:2]>/<h>/v000001.bin      # content of version 1, ...
    tmp/                                 # same-filesystem staging for atomic renames

Keys are hashed into directory names, so no key can ever escape ``root`` (no path
traversal, no collisions between ``a`` and ``a/b``). Writes are atomic (stage, fsync,
``os.replace``); reads re-hash content and fail with ``ChecksumMismatch`` on corruption.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from omf.adapters.storage._common import hash_stream, spool_and_hash, validate_metadata
from omf.core.domain.errors import ArtifactNotFound, ChecksumMismatch, StorageError
from omf.core.domain.models import HealthStatus, StorageArtifact, StorageStatus, utcnow
from omf.core.ports.storage import ArtifactStorageProvider, validate_key, validate_prefix

ADAPTER_TYPE = "local_fs"


class LocalFilesystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = "./var/artifacts"
    keep_versions: int = Field(default=3, ge=1, le=100)


class LocalFilesystemStorageAdapter(ArtifactStorageProvider):
    adapter_type = ADAPTER_TYPE

    def __init__(self, storage_provider_id: str, config: LocalFilesystemConfig) -> None:
        super().__init__(storage_provider_id)
        self._config = config
        self._root = Path(config.root).resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ paths

    def _dir(self, key: str) -> Path:
        h = hashlib.sha256(validate_key(key).encode()).hexdigest()
        return self._root / "objects" / h[:2] / h

    def _ensure_dirs(self) -> None:
        (self._root / "objects").mkdir(parents=True, exist_ok=True)
        (self._root / "tmp").mkdir(parents=True, exist_ok=True)

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any], tmp_dir: Path) -> None:
        tmp = tmp_dir / f".{path.parent.name}.{os.getpid()}.json"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _read_meta(self, key: str) -> dict[str, Any]:
        meta_path = self._dir(key) / "meta.json"
        if not meta_path.exists():
            raise ArtifactNotFound(
                f"artifact {key!r} not found",
                details={"storage_provider_id": self.storage_provider_id, "key": key},
            )
        return dict(json.loads(meta_path.read_text(encoding="utf-8")))

    def _artifact(self, meta: dict[str, Any], version: dict[str, Any]) -> StorageArtifact:
        return StorageArtifact(
            storage_provider_id=self.storage_provider_id,
            key=meta["key"],
            size_bytes=version["size"],
            sha256=version["sha256"],
            version=version["version"],
            content_type=version.get("content_type", "application/octet-stream"),
            created_at=datetime.fromisoformat(version["created_at"]),
            metadata=version.get("metadata", {}),
        )

    # ------------------------------------------------------------------ port

    async def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StorageArtifact:
        validate_key(key)
        metadata = validate_metadata(metadata)
        async with self._lock(key):
            return await asyncio.to_thread(self._put_sync, key, source, content_type, metadata)

    def _put_sync(
        self, key: str, source: BinaryIO, content_type: str, metadata: dict[str, str]
    ) -> StorageArtifact:
        self._ensure_dirs()
        directory = self._dir(key)
        directory.mkdir(parents=True, exist_ok=True)
        meta_path = directory / "meta.json"
        meta: dict[str, Any] = (
            json.loads(meta_path.read_text())
            if meta_path.exists()
            else {"key": key, "versions": []}
        )
        number = (int(meta["versions"][-1]["version"]) + 1) if meta["versions"] else 1
        blob_name = f"v{number:06d}.bin"
        tmp, size, sha = spool_and_hash(source, str(self._root / "tmp"))
        try:
            staged = self._root / "tmp" / f"{directory.name}.{blob_name}"
            with open(staged, "wb") as out:
                shutil.copyfileobj(tmp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(staged, directory / blob_name)
        finally:
            tmp.close()
        version = {
            "version": str(number),
            "blob": blob_name,
            "sha256": sha,
            "size": size,
            "content_type": content_type,
            "metadata": metadata,
            "created_at": utcnow().isoformat(),
        }
        meta["versions"].append(version)
        pruned = meta["versions"][: -self._config.keep_versions]
        meta["versions"] = meta["versions"][-self._config.keep_versions :]
        self._write_json_atomic(meta_path, meta, self._root / "tmp")
        for old in pruned:
            (directory / old["blob"]).unlink(missing_ok=True)
        return self._artifact(meta, version)

    async def get(self, key: str, destination: BinaryIO) -> StorageArtifact:
        return await asyncio.to_thread(self._get_sync, key, destination)

    def _get_sync(self, key: str, destination: BinaryIO) -> StorageArtifact:
        meta = self._read_meta(key)
        version = meta["versions"][-1]
        blob = self._dir(key) / version["blob"]
        try:
            with open(blob, "rb") as fh:
                _, sha = hash_stream(fh, destination)
        except FileNotFoundError as exc:
            raise StorageError(f"artifact {key!r} content is missing") from exc
        if sha != version["sha256"]:
            raise ChecksumMismatch(
                f"artifact {key!r} is corrupted (checksum mismatch)",
                details={"key": key, "expected": version["sha256"], "actual": sha},
            )
        return self._artifact(meta, version)

    async def list(self, prefix: str = "") -> list[StorageArtifact]:
        validate_prefix(prefix)
        return await asyncio.to_thread(self._list_sync, prefix)

    def _list_sync(self, prefix: str) -> builtins.list[StorageArtifact]:
        objects = self._root / "objects"
        if not objects.exists():
            return []
        out = []
        for meta_path in objects.glob("*/*/meta.json"):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta["key"].startswith(prefix) and meta["versions"]:
                out.append(self._artifact(meta, meta["versions"][-1]))
        return sorted(out, key=lambda a: a.key)

    async def delete(self, key: str) -> None:
        async with self._lock(key):
            await asyncio.to_thread(self._delete_sync, key)

    def _delete_sync(self, key: str) -> None:
        self._read_meta(key)  # raises ArtifactNotFound
        shutil.rmtree(self._dir(key))

    async def get_metadata(self, key: str) -> StorageArtifact:
        meta = await asyncio.to_thread(self._read_meta, key)
        return self._artifact(meta, meta["versions"][-1])

    async def verify_checksum(self, key: str, expected_sha256: str | None = None) -> bool:
        def check() -> bool:
            meta = self._read_meta(key)
            version = meta["versions"][-1]
            with open(self._dir(key) / version["blob"], "rb") as fh:
                _, sha = hash_stream(fh)
            return sha == (expected_sha256 or version["sha256"]) and sha == version["sha256"]

        return await asyncio.to_thread(check)

    async def status(self) -> StorageStatus:
        def probe() -> StorageStatus:
            try:
                self._ensure_dirs()
                usage = shutil.disk_usage(self._root)
                writable = os.access(self._root, os.W_OK)
            except OSError as exc:
                return StorageStatus(
                    storage_provider_id=self.storage_provider_id,
                    adapter_type=ADAPTER_TYPE,
                    status=HealthStatus.DOWN,
                    message=f"{type(exc).__name__}: {exc.strerror}",
                )
            return StorageStatus(
                storage_provider_id=self.storage_provider_id,
                adapter_type=ADAPTER_TYPE,
                status=HealthStatus.UP if writable else HealthStatus.DEGRADED,
                message=None if writable else "root is not writable",
                details={
                    "root": str(self._root),
                    "free_bytes": usage.free,
                    "keep_versions": self._config.keep_versions,
                },
                intended_use=["model_artifacts", "experiment_outputs", "benchmark_exports"],
            )

        return await asyncio.to_thread(probe)
