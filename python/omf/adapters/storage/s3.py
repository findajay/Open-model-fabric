"""S3-compatible artifact storage (AWS S3, MinIO, Ceph RGW, Cloudflare R2, ...).

Objects are stored at ``<prefix><key>``; the SHA-256 and the OMF version counter are kept
in object metadata (``x-amz-meta-omf-sha256`` / ``-omf-version``). Large files use
boto3's managed multipart transfers. Enable bucket versioning to retain old versions;
OMF itself tracks only the latest version's counter.

Known limitation: the version counter is read-then-written, so two concurrent writers to
the same key from *different processes* can produce the same version number. Content
integrity is unaffected (checksums are per object).
"""

from __future__ import annotations

import asyncio
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from omf.adapters.storage._common import HashingWriter, spool_and_hash, validate_metadata
from omf.core.domain.errors import (
    ArtifactNotFound,
    ChecksumMismatch,
    ConfigurationError,
    StorageError,
)
from omf.core.domain.models import HealthStatus, StorageArtifact, StorageStatus
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.ports.storage import ArtifactStorageProvider, validate_key, validate_prefix

ADAPTER_TYPE = "s3"


class S3Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    prefix: str = Field(default="omf/", pattern=r"^([A-Za-z0-9._-]+/)*$")
    region: str | None = None
    endpoint_url: str | None = Field(default=None, description="Set for MinIO / non-AWS endpoints")
    access_key_id_ref: SecretRefStr | None = None
    secret_access_key_ref: SecretRefStr | None = None
    force_path_style: bool = False


class S3StorageAdapter(ArtifactStorageProvider):
    adapter_type = ADAPTER_TYPE

    def __init__(self, storage_provider_id: str, config: S3Config, client: Any) -> None:
        super().__init__(storage_provider_id)
        self._config = config
        self._s3 = client

    @classmethod
    def from_config(
        cls,
        storage_provider_id: str,
        config: S3Config,
        access_key: SecretValue | None,
        secret_key: SecretValue | None,
    ) -> S3StorageAdapter:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError(
                "S3 storage needs the 's3' extra: pip install 'open-model-fabric[s3]'"
            ) from exc
        kwargs: dict[str, Any] = {
            "region_name": config.region,
            "endpoint_url": config.endpoint_url,
            "config": Config(
                s3={"addressing_style": "path" if config.force_path_style else "auto"}
            ),
        }
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key.reveal()
            kwargs["aws_secret_access_key"] = secret_key.reveal()
        return cls(storage_provider_id, config, boto3.client("s3", **kwargs))

    def _object_key(self, key: str) -> str:
        return self._config.prefix + validate_key(key)

    def _error(self, exc: Exception, key: str | None = None) -> Exception:
        code = (
            getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if hasattr(exc, "response")
            else ""
        )
        if code in ("404", "NoSuchKey", "NotFound"):
            return ArtifactNotFound(
                f"artifact {key!r} not found",
                details={"storage_provider_id": self.storage_provider_id, "key": key},
            )
        return StorageError(
            f"S3 error: {code or type(exc).__name__}",
            details={"storage_provider_id": self.storage_provider_id},
            retryable=code in ("SlowDown", "ServiceUnavailable", "InternalError"),
        )

    def _artifact(self, key: str, head: dict[str, Any]) -> StorageArtifact:
        meta = head.get("Metadata", {})
        return StorageArtifact(
            storage_provider_id=self.storage_provider_id,
            key=key,
            size_bytes=int(head["ContentLength"]),
            sha256=meta.get("omf-sha256", ""),
            version=meta.get("omf-version", "1"),
            content_type=head.get("ContentType", "application/octet-stream"),
            created_at=head["LastModified"],
            metadata={k[5:]: v for k, v in meta.items() if k.startswith("user-")},
        )

    def _head(self, key: str) -> dict[str, Any]:
        try:
            return dict(self._s3.head_object(Bucket=self._config.bucket, Key=self._object_key(key)))
        except Exception as exc:
            raise self._error(exc, key) from exc

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

        def run() -> StorageArtifact:
            tmp, _size, sha = spool_and_hash(source)
            try:
                try:
                    previous = int(self._head(key).get("Metadata", {}).get("omf-version", "0"))
                except ArtifactNotFound:
                    previous = 0
                extra = {
                    "ContentType": content_type,
                    "Metadata": {
                        "omf-sha256": sha,
                        "omf-version": str(previous + 1),
                        **{f"user-{k}": v for k, v in metadata.items()},
                    },
                }
                try:
                    self._s3.upload_fileobj(
                        tmp, self._config.bucket, self._object_key(key), ExtraArgs=extra
                    )
                except Exception as exc:
                    raise self._error(exc, key) from exc
            finally:
                tmp.close()
            return self._artifact(key, self._head(key))

        return await asyncio.to_thread(run)

    async def get(self, key: str, destination: BinaryIO) -> StorageArtifact:
        def run() -> StorageArtifact:
            artifact = self._artifact(key, self._head(key))
            writer = HashingWriter(destination)
            try:
                self._s3.download_fileobj(self._config.bucket, self._object_key(key), writer)
            except Exception as exc:
                raise self._error(exc, key) from exc
            if artifact.sha256 and writer.sha256 != artifact.sha256:
                raise ChecksumMismatch(
                    f"artifact {key!r} failed checksum verification",
                    details={"key": key, "expected": artifact.sha256, "actual": writer.sha256},
                )
            return artifact

        return await asyncio.to_thread(run)

    async def list(self, prefix: str = "") -> list[StorageArtifact]:
        validate_prefix(prefix)

        def run() -> list[StorageArtifact]:
            out = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._config.bucket, Prefix=self._config.prefix + prefix
            ):
                for obj in page.get("Contents", []):
                    key = obj["Key"][len(self._config.prefix) :]
                    out.append(self._artifact(key, self._head(key)))
            return sorted(out, key=lambda a: a.key)

        try:
            return await asyncio.to_thread(run)
        except ArtifactNotFound:
            return []

    async def delete(self, key: str) -> None:
        def run() -> None:
            self._head(key)
            self._s3.delete_object(Bucket=self._config.bucket, Key=self._object_key(key))

        await asyncio.to_thread(run)

    async def get_metadata(self, key: str) -> StorageArtifact:
        return await asyncio.to_thread(lambda: self._artifact(key, self._head(key)))

    async def verify_checksum(self, key: str, expected_sha256: str | None = None) -> bool:
        def run() -> bool:
            recorded = self._head(key).get("Metadata", {}).get("omf-sha256")
            writer = HashingWriter()
            self._s3.download_fileobj(self._config.bucket, self._object_key(key), writer)
            return writer.sha256 == (expected_sha256 or recorded) and writer.sha256 == recorded

        return await asyncio.to_thread(run)

    async def status(self) -> StorageStatus:
        def run() -> StorageStatus:
            base = {
                "storage_provider_id": self.storage_provider_id,
                "adapter_type": ADAPTER_TYPE,
                "intended_use": ["model_artifacts", "model_serving_cache", "experiment_outputs"],
            }
            try:
                self._s3.head_bucket(Bucket=self._config.bucket)
            except Exception as exc:
                return StorageStatus(
                    **base, status=HealthStatus.DOWN, message=str(self._error(exc))
                )
            return StorageStatus(
                **base,
                status=HealthStatus.UP,
                details={
                    "bucket": self._config.bucket,
                    "prefix": self._config.prefix,
                    "endpoint": self._config.endpoint_url or "aws",
                },
            )

        return await asyncio.to_thread(run)
