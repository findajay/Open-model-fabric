"""Google Drive artifact storage (Drive API v3 over REST).

Intended use: model artifacts and experiment outputs shared with Colab notebooks. Drive
is **not** a model-serving store (no range-optimised reads, per-user quotas, API rate
limits); serve weights from local disk or object storage instead.

Design:

* All files live directly inside one configured folder (``folder_id``), tagged with
  ``appProperties``: ``omf_ns`` (namespace), ``omf_kh`` (SHA-256 of the key, used for
  exact lookups because appProperty values are limited to ~124 bytes),
  ``omf_sha256`` and ``omf_version``. The file ``name`` is the full key.
* Uploads use the resumable protocol in chunks (multiple of 256 KiB), so files of any
  size can be stored without holding them in memory. Re-putting a key uploads a new
  revision of the same Drive file and increments ``omf_version``.
* Downloads are hashed on the fly and fail with ``ChecksumMismatch`` if content does not
  match the recorded SHA-256.
* ``delete`` moves the file to the Drive trash (recoverable) instead of destroying it.
* Authentication: a service-account JSON referenced by ``credentials_ref`` (secret
  reference, never inline). Scope defaults to ``drive.file`` (least privilege: only files
  this app created). Share the folder with the service account's e-mail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import IO, Any, BinaryIO

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omf.adapters.storage._common import HashingWriter, spool_and_hash, validate_metadata
from omf.core.domain.errors import (
    ArtifactNotFound,
    ChecksumMismatch,
    ConfigurationError,
    OMFError,
    StorageError,
)
from omf.core.domain.models import HealthStatus, StorageArtifact, StorageStatus
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.ports.storage import ArtifactStorageProvider, validate_key, validate_prefix

ADAPTER_TYPE = "google_drive"
_CHUNK_UNIT = 256 * 1024
_FIELDS = "id,name,size,mimeType,createdTime,modifiedTime,appProperties,trashed"

TokenSource = Callable[[], Awaitable[str]]


class GoogleDriveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: str = Field(pattern=r"^[A-Za-z0-9_-]{10,200}$")
    credentials_ref: SecretRefStr
    namespace: str = Field(default="omf", pattern=r"^[a-z0-9_-]{1,32}$")
    scope: str = "https://www.googleapis.com/auth/drive.file"
    chunk_size_mb: int = Field(default=8, ge=1, le=256)
    api_base: str = "https://www.googleapis.com"
    timeout_s: float = Field(default=60.0, gt=0, le=600)


class ServiceAccountTokenSource:
    """Mints OAuth access tokens from a service-account JSON using ``google-auth``."""

    def __init__(self, credentials_json: SecretValue, scope: str) -> None:
        try:
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ConfigurationError(
                "Google Drive storage needs the 'gdrive' extra: "
                "pip install 'open-model-fabric[gdrive]'"
            ) from exc
        try:
            info = json.loads(credentials_json.reveal())
        except ValueError as exc:
            raise ConfigurationError("Google Drive credentials secret is not valid JSON") from exc
        self._credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=[scope]
        )
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if not self._credentials.valid:
                from google.auth.transport.requests import Request

                await asyncio.to_thread(self._credentials.refresh, Request())
            return str(self._credentials.token)


class GoogleDriveStorageAdapter(ArtifactStorageProvider):
    adapter_type = ADAPTER_TYPE

    def __init__(
        self,
        storage_provider_id: str,
        config: GoogleDriveConfig,
        token_source: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(storage_provider_id)
        self._config = config
        self._token = token_source
        self._chunk = config.chunk_size_mb * 1024 * 1024 // _CHUNK_UNIT * _CHUNK_UNIT
        # Redirects are followed only to Google-owned hosts (Drive media downloads may be
        # served from googleusercontent.com); anything else is refused, not fetched.
        api_host = httpx.URL(config.api_base).host
        self._allowed_hosts = (api_host, ".googleusercontent.com", ".googleapis.com")
        self._client = httpx.AsyncClient(
            base_url=config.api_base,
            timeout=config.timeout_s,
            follow_redirects=True,
            transport=transport,
            event_hooks={"request": [self._check_host]},
        )
        self._locks: dict[str, asyncio.Lock] = {}

    async def _check_host(self, request: httpx.Request) -> None:
        host = request.url.host
        if not any(
            host == h or (h.startswith(".") and host.endswith(h)) for h in self._allowed_hosts
        ):
            raise StorageError(f"refusing to contact unexpected host {host!r} (redirect?)")

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _request(
        self, method: str, url: str, *, retries: int = 3, **kwargs: Any
    ) -> httpx.Response:
        for attempt in range(retries + 1):
            headers = {
                **kwargs.pop("headers", {}),
                "Authorization": f"Bearer {await self._token()}",
            }
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= retries:
                    raise StorageError(
                        f"Google Drive unreachable ({type(exc).__name__})", retryable=True
                    ) from exc
            else:
                if response.status_code not in (429, 500, 502, 503, 504) or attempt >= retries:
                    return response
            kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
            await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))
        raise AssertionError("unreachable")

    def _error(self, response: httpx.Response, key: str | None = None) -> OMFError:
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = response.text[:200]
        details = {"storage_provider_id": self.storage_provider_id, "status": response.status_code}
        if response.status_code == 404:
            return ArtifactNotFound(
                f"artifact {key!r} not found" if key else "not found", details=details
            )
        retryable = response.status_code in (429, 500, 502, 503, 504)
        return StorageError(
            f"Google Drive error {response.status_code}: {message[:200]}",
            details=details,
            retryable=retryable,
        )

    # ------------------------------------------------------------------ lookup

    def _base_query(self) -> str:
        c = self._config
        return (
            f"'{c.folder_id}' in parents and trashed = false and "
            f"appProperties has {{ key='omf_ns' and value='{c.namespace}' }}"
        )

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:40]

    async def _files(self, query: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({_FIELDS})",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            response = await self._request("GET", "/drive/v3/files", params=params)
            if response.status_code != 200:
                raise self._error(response)
            payload = response.json()
            files.extend(payload.get("files", []))
            page = payload.get("nextPageToken")
            if not page:
                return files

    async def _find(self, key: str) -> dict[str, Any]:
        query = (
            self._base_query()
            + f" and appProperties has {{ key='omf_kh' and value='{self._key_hash(key)}' }}"
        )
        matches = [f for f in await self._files(query) if f.get("name") == key]
        if not matches:
            raise ArtifactNotFound(
                f"artifact {key!r} not found",
                details={"storage_provider_id": self.storage_provider_id, "key": key},
            )
        return max(matches, key=lambda f: f.get("modifiedTime", ""))

    def _artifact(self, f: dict[str, Any]) -> StorageArtifact:
        props = f.get("appProperties") or {}
        user_meta = {k[5:]: v for k, v in props.items() if k.startswith("meta_")}
        return StorageArtifact(
            storage_provider_id=self.storage_provider_id,
            key=f["name"],
            size_bytes=int(f.get("size", 0)),
            sha256=props.get("omf_sha256", ""),
            version=props.get("omf_version", "1"),
            content_type=f.get("mimeType", "application/octet-stream"),
            created_at=datetime.fromisoformat(
                str(f.get("modifiedTime") or f.get("createdTime")).replace("Z", "+00:00")
            ),
            metadata=user_meta,
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
        for k, v in metadata.items():
            if len(f"meta_{k}".encode()) + len(v.encode()) > 124:
                raise OMFError(f"metadata entry {k!r} too long for Drive appProperties (124 bytes)")
        async with self._locks.setdefault(key, asyncio.Lock()):
            tmp, size, sha = await asyncio.to_thread(spool_and_hash, source)
            try:
                try:
                    existing: dict[str, Any] | None = await self._find(key)
                except ArtifactNotFound:
                    existing = None
                version = int((existing or {}).get("appProperties", {}).get("omf_version", "0")) + 1
                props = {
                    "omf_ns": self._config.namespace,
                    "omf_kh": self._key_hash(key),
                    "omf_sha256": sha,
                    "omf_version": str(version),
                    **{f"meta_{k}": v for k, v in metadata.items()},
                }
                body: dict[str, Any] = {"mimeType": content_type, "appProperties": props}
                params = {"uploadType": "resumable", "supportsAllDrives": "true", "fields": _FIELDS}
                headers = {
                    "X-Upload-Content-Type": content_type,
                    "X-Upload-Content-Length": str(size),
                }
                if existing is None:
                    body |= {"name": key, "parents": [self._config.folder_id]}
                    init = await self._request(
                        "POST", "/upload/drive/v3/files", params=params, json=body, headers=headers
                    )
                else:
                    init = await self._request(
                        "PATCH",
                        f"/upload/drive/v3/files/{existing['id']}",
                        params=params,
                        json=body,
                        headers=headers,
                    )
                if init.status_code != 200 or "location" not in init.headers:
                    raise self._error(init, key)
                result = await self._upload(init.headers["location"], tmp, size)
            finally:
                tmp.close()
        if result.get("appProperties", {}).get("omf_sha256") != sha:
            raise StorageError("Google Drive did not store the expected checksum metadata")
        return self._artifact(result)

    async def _upload(self, session_url: str, tmp: IO[bytes], size: int) -> dict[str, Any]:
        offset = 0
        failures = 0
        while True:
            await asyncio.to_thread(tmp.seek, offset)
            chunk = await asyncio.to_thread(tmp.read, self._chunk)
            end = offset + len(chunk) - 1
            content_range = f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"
            response = await self._request(
                "PUT",
                session_url,
                retries=0,
                content=chunk,
                headers={"Content-Range": content_range},
            )
            if response.status_code in (200, 201):
                return dict(response.json())
            if response.status_code == 308:
                failures = 0
                committed = response.headers.get("range")  # e.g. "bytes=0-8388607"
                offset = int(committed.rsplit("-", 1)[1]) + 1 if committed else 0
                continue
            failures += 1
            if (response.status_code < 500 and response.status_code != 429) or failures > 3:
                raise self._error(response)
            # Ask the server how much it committed, then resume from there.
            await asyncio.sleep(0.5 * 2**failures)
            status = await self._request(
                "PUT",
                session_url,
                retries=0,
                content=b"",
                headers={"Content-Range": f"bytes */{size}"},
            )
            if status.status_code in (200, 201):
                return dict(status.json())
            committed = status.headers.get("range")
            offset = int(committed.rsplit("-", 1)[1]) + 1 if committed else 0

    async def get(self, key: str, destination: BinaryIO) -> StorageArtifact:
        validate_key(key)
        f = await self._find(key)
        artifact = self._artifact(f)
        writer = HashingWriter(destination)
        await self._download(f["id"], writer, key)
        if artifact.sha256 and writer.sha256 != artifact.sha256:
            raise ChecksumMismatch(
                f"artifact {key!r} failed checksum verification",
                details={"key": key, "expected": artifact.sha256, "actual": writer.sha256},
            )
        return artifact

    async def _download(self, file_id: str, writer: HashingWriter, key: str) -> None:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        async with self._client.stream(
            "GET",
            f"/drive/v3/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise self._error(response, key)
            async for chunk in response.aiter_bytes(1024 * 1024):
                await asyncio.to_thread(writer.write, chunk)

    async def list(self, prefix: str = "") -> list[StorageArtifact]:
        validate_prefix(prefix)
        files = await self._files(self._base_query())
        latest: dict[str, dict[str, Any]] = {}
        for f in files:
            name = f.get("name", "")
            if name.startswith(prefix) and (
                name not in latest
                or f.get("modifiedTime", "") > latest[name].get("modifiedTime", "")
            ):
                latest[name] = f
        return sorted((self._artifact(f) for f in latest.values()), key=lambda a: a.key)

    async def delete(self, key: str) -> None:
        validate_key(key)
        f = await self._find(key)
        response = await self._request(
            "PATCH",
            f"/drive/v3/files/{f['id']}",
            params={"supportsAllDrives": "true"},
            json={"trashed": True},
        )
        if response.status_code != 200:
            raise self._error(response, key)

    async def get_metadata(self, key: str) -> StorageArtifact:
        validate_key(key)
        return self._artifact(await self._find(key))

    async def verify_checksum(self, key: str, expected_sha256: str | None = None) -> bool:
        f = await self._find(validate_key(key))
        recorded = (f.get("appProperties") or {}).get("omf_sha256")
        writer = HashingWriter()
        await self._download(f["id"], writer, key)
        return writer.sha256 == (expected_sha256 or recorded) and writer.sha256 == recorded

    async def status(self) -> StorageStatus:
        started = time.perf_counter()
        base = {
            "storage_provider_id": self.storage_provider_id,
            "adapter_type": ADAPTER_TYPE,
            "intended_use": ["model_artifacts", "experiment_outputs", "benchmark_exports"],
        }
        try:
            response = await self._request(
                "GET",
                f"/drive/v3/files/{self._config.folder_id}",
                retries=1,
                params={
                    "fields": "id,name,mimeType,trashed,capabilities/canAddChildren",
                    "supportsAllDrives": "true",
                },
            )
        except OMFError as exc:
            return StorageStatus(**base, status=HealthStatus.DOWN, message=exc.message)
        if response.status_code != 200:
            return StorageStatus(
                **base, status=HealthStatus.DOWN, message=self._error(response).message
            )
        folder = response.json()
        can_write = (folder.get("capabilities") or {}).get("canAddChildren", False)
        return StorageStatus(
            **base,
            status=HealthStatus.UP
            if can_write and not folder.get("trashed")
            else HealthStatus.DEGRADED,
            message=None if can_write else "service account cannot add files to the folder",
            details={
                "folder_name": folder.get("name"),
                "namespace": self._config.namespace,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "note": "Drive is for artifacts and experiment outputs, "
                "not production model serving.",
            },
        )
