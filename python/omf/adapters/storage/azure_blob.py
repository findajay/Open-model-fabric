"""Azure Blob Storage artifact storage (REST API over httpx; no Azure SDK).

Objects are stored as block blobs at ``<container>/<prefix><key>``. The SHA-256 and the
OMF version counter are kept in blob metadata (``omf_sha256`` / ``omf_version``); user
metadata is stored as one base64-encoded JSON value (``omf_user``) because Azure
metadata names must be C# identifiers (no ``-``) and are case-insensitive. Blobs larger
than ``block_size_mb`` are uploaded as blocks plus a block list, so multi-gigabyte
weights are never held in memory.

Authentication (exactly one):

* **Shared Key**: ``account_key_ref`` (the base64 account key). Requests are signed with
  HMAC-SHA256 over the documented string-to-sign: verb, eleven standard headers, the
  canonicalized ``x-ms-*`` headers, and the canonicalized resource. The header ordering
  follows the service's collation, ported from azure-sdk-for-python (MIT).
* **SAS token**: ``sas_token_ref`` (the query string of a container or account SAS).
* **Entra ID service principal**: ``tenant_id`` + ``client_id`` + ``client_secret_ref``
  (OAuth2 client credentials, scope ``https://storage.azure.com/.default``).

The endpoint defaults to ``https://<account>.blob.core.windows.net``. For Azurite use
``endpoint_url: http://127.0.0.1:10000/devstoreaccount1`` (path-style). Endpoints are
subject to the egress policy, and redirects are never followed.

Known limitation (as for S3): the version counter is read-then-written, so concurrent
writers in different processes can produce the same version number. Content integrity
is unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from functools import cmp_to_key
from typing import IO, Any, BinaryIO
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omf.adapters.storage._common import CHUNK, HashingWriter, spool_and_hash, validate_metadata
from omf.core.domain.errors import (
    ArtifactNotFound,
    ChecksumMismatch,
    OMFError,
    StorageError,
)
from omf.core.domain.models import HealthStatus, StorageArtifact, StorageStatus
from omf.core.ports.secrets import SecretRefStr, SecretValue
from omf.core.ports.storage import ArtifactStorageProvider, validate_key, validate_prefix
from omf.core.security.egress import EgressPolicy

ADAPTER_TYPE = "azure_blob"
API_VERSION = "2023-11-03"
STORAGE_SCOPE = "https://storage.azure.com/.default"
INTENDED_USE = ["model_artifacts", "model_serving_cache", "experiment_outputs"]
# The eleven standard headers of the Shared Key string-to-sign, in order. Range is sent
# as x-ms-range (a canonicalized header), so its slot stays empty, as in the Azure SDK.
_SIGNED_HEADERS = (
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-type",
    "date",
    "if-modified-since",
    "if-match",
    "if-none-match",
    "if-unmodified-since",
    "range",
)


class AzureBlobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str = Field(pattern=r"^[a-z0-9]{3,24}$")
    container: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    prefix: str = Field(default="omf/", pattern=r"^([A-Za-z0-9._-]+/)*$")
    endpoint_url: str | None = Field(
        default=None,
        description="Blob endpoint; default https://<account>.blob.core.windows.net. "
        "Azurite: http://127.0.0.1:10000/devstoreaccount1",
    )
    account_key_ref: SecretRefStr | None = None
    sas_token_ref: SecretRefStr | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret_ref: SecretRefStr | None = None
    authority_url: str = "https://login.microsoftonline.com"
    block_size_mb: int = Field(default=8, ge=1, le=100)
    timeout_s: float = Field(default=120.0, gt=0, le=3600)

    @field_validator("container")
    @classmethod
    def _no_double_hyphen(cls, v: str) -> str:
        if "--" in v:
            raise ValueError("container names cannot contain consecutive hyphens")
        return v

    @model_validator(mode="after")
    def _one_auth(self) -> AzureBlobConfig:
        entra = bool(self.tenant_id or self.client_id or self.client_secret_ref)
        if entra and not (self.tenant_id and self.client_id and self.client_secret_ref):
            raise ValueError("Entra ID needs tenant_id, client_id and client_secret_ref")
        methods = [bool(self.account_key_ref), bool(self.sas_token_ref), entra]
        if sum(methods) != 1:
            raise ValueError(
                "configure exactly one of: account_key_ref, sas_token_ref, "
                "or tenant_id + client_id + client_secret_ref"
            )
        return self

    @property
    def endpoint(self) -> str:
        return (self.endpoint_url or f"https://{self.account_name}.blob.core.windows.net").rstrip(
            "/"
        )


# ---------------------------------------------------------------------- Shared Key


# Collation weights used by the service to sort x-ms-* headers (level 0: '-' is ignored,
# '_' sorts before digits and letters). Ported from azure-sdk-for-python
# (sdk/storage/azure-storage-blob/azure/storage/blob/_shared/authentication.py, MIT).
_LV0 = [0] * 33 + [
    0x71C, 0x0, 0x71F, 0x721, 0x723, 0x725, 0x0, 0x0, 0x0, 0x72D, 0x803, 0x0, 0x0, 0x733,
    0x0, 0xD03, 0xD1A, 0xD1C, 0xD1E, 0xD20, 0xD22, 0xD24, 0xD26, 0xD28, 0xD2A, 0x0, 0x0,
    0x0, 0x0, 0x0, 0x0, 0x0, 0xE02, 0xE09, 0xE0A, 0xE1A, 0xE21, 0xE23, 0xE25, 0xE2C, 0xE32,
    0xE35, 0xE36, 0xE48, 0xE51, 0xE70, 0xE7C, 0xE7E, 0xE89, 0xE8A, 0xE91, 0xE99, 0xE9F,
    0xEA2, 0xEA4, 0xEA6, 0xEA7, 0xEA9, 0x0, 0x0, 0x0, 0x743, 0x744, 0x748, 0xE02, 0xE09,
    0xE0A, 0xE1A, 0xE21, 0xE23, 0xE25, 0xE2C, 0xE32, 0xE35, 0xE36, 0xE48, 0xE51, 0xE70,
    0xE7C, 0xE7E, 0xE89, 0xE8A, 0xE91, 0xE99, 0xE9F, 0xEA2, 0xEA4, 0xEA6, 0xEA7, 0xEA9,
    0x0, 0x74C, 0x0, 0x750, 0x0,
]  # fmt: skip
_LV4 = [0] * 128
_LV4[0x27] = 0x8012  # '
_LV4[0x2D] = 0x8212  # -


def _collate(lhs: str, rhs: str) -> int:
    tables = (_LV0, _LV4)
    level, i, j = 0, 0, 0
    while level < len(tables):
        if level == len(tables) - 1 and i != j:
            return -1 if i > j else 1
        w1 = tables[level][ord(lhs[i])] if i < len(lhs) else 0x1
        w2 = tables[level][ord(rhs[j])] if j < len(rhs) else 0x1
        if w1 == 0x1 and w2 == 0x1:
            i = j = 0
            level += 1
        elif w1 == w2:
            i += 1
            j += 1
        elif w1 == 0:
            i += 1
        elif w2 == 0:
            j += 1
        else:
            return -1 if w1 < w2 else 1
    return 0


_HEADER_ORDER = cmp_to_key(_collate)


def string_to_sign(account: str, method: str, url: str, headers: dict[str, str]) -> str:
    """The Shared Key string-to-sign for a Blob service request (version 2009-09-19+)."""
    lowered = {k.lower(): v for k, v in headers.items() if v is not None}
    if lowered.get("content-length") == "0":
        lowered.pop("content-length")  # signed as empty for zero-length bodies
    standard = "\n".join(lowered.get(h, "") for h in _SIGNED_HEADERS) + "\n"
    ms_headers = sorted(
        ((k, v) for k, v in lowered.items() if k.startswith("x-ms-")),
        key=lambda item: _HEADER_ORDER(item[0]),
    )
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in ms_headers)
    parts = urlsplit(url)
    resource = f"/{account}{parts.path}"
    for name, value in sorted(parse_qsl(parts.query, keep_blank_values=True)):
        resource += f"\n{name.lower()}:{unquote(value)}"
    return f"{method.upper()}\n{standard}{canonical_headers}{resource}"


def shared_key_signature(account_key: str, to_sign: str) -> str:
    digest = hmac.new(base64.b64decode(account_key), to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _parse_listing(body: bytes) -> ET.Element:
    """Parse a List Blobs response. Entity-expansion attacks need a DTD and the Blob
    service never sends one, so any DOCTYPE is refused before parsing; ElementTree does
    not resolve external entities."""
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise StorageError("Azure Blob listing contained a DTD; refusing to parse it")
    try:
        return ET.fromstring(body)  # noqa: S314 - DTDs rejected above
    except ET.ParseError as exc:
        raise StorageError("Azure Blob returned an invalid listing") from exc


class EntraTokenSource:
    """OAuth2 client-credentials tokens for Azure Storage, cached until shortly before expiry."""

    def __init__(
        self,
        config: AzureBlobConfig,
        secret: SecretValue,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._secret = secret
        self._transport = transport
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires - 60:
                return self._token
            c = self._config
            async with httpx.AsyncClient(
                timeout=30, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"{c.authority_url}/{c.tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": c.client_id or "",
                        "client_secret": self._secret.reveal(),
                        "scope": STORAGE_SCOPE,
                    },
                )
            if response.status_code != 200:
                raise StorageError(
                    f"Azure authentication failed (HTTP {response.status_code})",
                    details={"status": response.status_code},
                )
            body = response.json()
            self._token = str(body["access_token"])
            self._expires = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token


# ---------------------------------------------------------------------- adapter


class AzureBlobStorageAdapter(ArtifactStorageProvider):
    adapter_type = ADAPTER_TYPE

    def __init__(
        self,
        storage_provider_id: str,
        config: AzureBlobConfig,
        egress: EgressPolicy,
        *,
        account_key: SecretValue | None = None,
        sas_token: SecretValue | None = None,
        client_secret: SecretValue | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(storage_provider_id)
        self._config = config
        self._egress = egress
        self._egress_checked = False
        self._account_key = account_key
        self._sas = sas_token.reveal().lstrip("?") if sas_token else None
        self._token = (
            EntraTokenSource(config, client_secret, transport=transport) if client_secret else None
        )
        self._client = httpx.AsyncClient(
            timeout=config.timeout_s, follow_redirects=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ HTTP

    def _url(self, blob: str | None = None) -> str:
        base = f"{self._config.endpoint}/{self._config.container}"
        if blob is None:
            return base
        return f"{base}/{quote(self._config.prefix + blob, safe='/~')}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        if not self._egress_checked:
            await self._egress.check_url(self._config.endpoint)
            self._egress_checked = True
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in (params or {}).items())
        if self._sas:
            query = f"{query}&{self._sas}" if query else self._sas
        full = f"{url}?{query}" if query else url
        hdrs = {
            "x-ms-version": API_VERSION,
            "x-ms-date": format_datetime(datetime.now(UTC), usegmt=True),
            **(headers or {}),
        }
        if content is not None:
            hdrs["Content-Length"] = str(len(content))
        elif method in ("PUT", "POST"):
            hdrs["Content-Length"] = "0"
        if self._account_key is not None:
            to_sign = string_to_sign(self._config.account_name, method, full, hdrs)
            signature = shared_key_signature(self._account_key.reveal(), to_sign)
            hdrs["Authorization"] = f"SharedKey {self._config.account_name}:{signature}"
        elif self._token is not None:
            hdrs["Authorization"] = f"Bearer {await self._token()}"
        request = self._client.build_request(method, full, headers=hdrs, content=content)
        try:
            return await self._client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise StorageError(
                f"Azure Blob endpoint unreachable ({type(exc).__name__})",
                details={"storage_provider_id": self.storage_provider_id},
                retryable=True,
            ) from exc

    def _error(self, response: httpx.Response, key: str | None = None) -> OMFError:
        code = response.headers.get("x-ms-error-code", "")
        if response.status_code == 404 and code in ("", "BlobNotFound"):
            return ArtifactNotFound(
                f"artifact {key!r} not found",
                details={"storage_provider_id": self.storage_provider_id, "key": key},
            )
        return StorageError(
            f"Azure Blob error: HTTP {response.status_code} {code}".strip(),
            details={
                "storage_provider_id": self.storage_provider_id,
                "status": response.status_code,
            },
            retryable=response.status_code in (408, 429, 500, 502, 503, 504),
        )

    async def _checked(self, response: httpx.Response, key: str | None = None) -> httpx.Response:
        if response.status_code >= 400:
            await response.aread()
            raise self._error(response, key)
        return response

    # ------------------------------------------------------------------ mapping

    def _artifact(
        self, key: str, size: int, content_type: str, modified: str, meta: dict[str, str]
    ) -> StorageArtifact:
        user: dict[str, str] = {}
        if meta.get("omf_user"):
            user = json.loads(base64.b64decode(meta["omf_user"]))
        return StorageArtifact(
            storage_provider_id=self.storage_provider_id,
            key=key,
            size_bytes=size,
            sha256=meta.get("omf_sha256", ""),
            version=meta.get("omf_version", "1"),
            content_type=content_type or "application/octet-stream",
            created_at=parsedate_to_datetime(modified) if modified else datetime.now(UTC),
            metadata=user,
        )

    async def _head(self, key: str) -> StorageArtifact:
        response = await self._checked(await self._request("HEAD", self._url(key)), key)
        h = response.headers
        meta = {k[10:].lower(): v for k, v in h.items() if k.lower().startswith("x-ms-meta-")}
        return self._artifact(
            key,
            int(h.get("content-length", "0")),
            h.get("content-type", ""),
            h.get("last-modified", ""),
            meta,
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
        tmp, size, sha = await asyncio.to_thread(spool_and_hash, source)
        try:
            try:
                previous = int((await self._head(key)).version)
            except ArtifactNotFound:
                previous = 0
            meta_headers = {
                "x-ms-meta-omf_sha256": sha,
                "x-ms-meta-omf_version": str(previous + 1),
            }
            if metadata:
                meta_headers["x-ms-meta-omf_user"] = base64.b64encode(
                    json.dumps(metadata, sort_keys=True).encode()
                ).decode()
            block_size = self._config.block_size_mb * 1024 * 1024
            if size <= block_size:
                body = await asyncio.to_thread(tmp.read)
                await self._checked(
                    await self._request(
                        "PUT",
                        self._url(key),
                        headers={
                            "x-ms-blob-type": "BlockBlob",
                            "Content-Type": content_type,
                            **meta_headers,
                        },
                        content=body,
                    ),
                    key,
                )
            else:
                await self._put_blocks(key, tmp, block_size, content_type, meta_headers)
        finally:
            tmp.close()
        return await self._head(key)

    async def _put_blocks(
        self,
        key: str,
        tmp: IO[bytes],
        block_size: int,
        content_type: str,
        meta_headers: dict[str, str],
    ) -> None:
        ids: list[str] = []
        while True:
            chunk = await asyncio.to_thread(tmp.read, block_size)
            if not chunk:
                break
            # Block ids must be base64 and of equal length within a blob.
            block_id = base64.b64encode(f"{len(ids):08d}".encode()).decode()
            await self._checked(
                await self._request(
                    "PUT",
                    self._url(key),
                    params={"comp": "block", "blockid": block_id},
                    content=chunk,
                ),
                key,
            )
            ids.append(block_id)
        block_list = (
            '<?xml version="1.0" encoding="utf-8"?><BlockList>'
            + "".join(f"<Latest>{i}</Latest>" for i in ids)
            + "</BlockList>"
        ).encode()
        await self._checked(
            await self._request(
                "PUT",
                self._url(key),
                params={"comp": "blocklist"},
                headers={"x-ms-blob-content-type": content_type, **meta_headers},
                content=block_list,
            ),
            key,
        )

    async def _download(self, key: str, writer: HashingWriter) -> None:
        response = await self._request("GET", self._url(key), stream=True)
        try:
            await self._checked(response, key)
            async for chunk in response.aiter_bytes(CHUNK):
                await asyncio.to_thread(writer.write, chunk)
        finally:
            await response.aclose()

    async def get(self, key: str, destination: BinaryIO) -> StorageArtifact:
        artifact = await self._head(key)
        writer = HashingWriter(destination)
        await self._download(key, writer)
        if artifact.sha256 and writer.sha256 != artifact.sha256:
            raise ChecksumMismatch(
                f"artifact {key!r} failed checksum verification",
                details={"key": key, "expected": artifact.sha256, "actual": writer.sha256},
            )
        return artifact

    async def list(self, prefix: str = "") -> list[StorageArtifact]:
        validate_prefix(prefix)
        out: list[StorageArtifact] = []
        marker = ""
        while True:
            params = {
                "restype": "container",
                "comp": "list",
                "include": "metadata",
                "prefix": self._config.prefix + prefix,
            }
            if marker:
                params["marker"] = marker
            response = await self._checked(await self._request("GET", self._url(), params=params))
            root = _parse_listing(response.content)
            for blob in root.iter("Blob"):
                name = blob.findtext("Name") or ""
                props = blob.find("Properties")
                meta_el = blob.find("Metadata")
                meta = (
                    {child.tag.lower(): child.text or "" for child in meta_el}
                    if meta_el is not None
                    else {}
                )
                out.append(
                    self._artifact(
                        name[len(self._config.prefix) :],
                        int(props.findtext("Content-Length") or 0) if props is not None else 0,
                        props.findtext("Content-Type") or "" if props is not None else "",
                        props.findtext("Last-Modified") or "" if props is not None else "",
                        meta,
                    )
                )
            marker = root.findtext("NextMarker") or ""
            if not marker:
                break
        return sorted(out, key=lambda a: a.key)

    async def delete(self, key: str) -> None:
        await self._head(key)  # ArtifactNotFound if absent
        await self._checked(
            await self._request(
                "DELETE", self._url(key), headers={"x-ms-delete-snapshots": "include"}
            ),
            key,
        )

    async def get_metadata(self, key: str) -> StorageArtifact:
        return await self._head(key)

    async def verify_checksum(self, key: str, expected_sha256: str | None = None) -> bool:
        recorded = (await self._head(key)).sha256
        writer = HashingWriter()
        await self._download(key, writer)
        return writer.sha256 == (expected_sha256 or recorded) and writer.sha256 == recorded

    async def status(self) -> StorageStatus:
        base: dict[str, Any] = {
            "storage_provider_id": self.storage_provider_id,
            "adapter_type": ADAPTER_TYPE,
            "intended_use": INTENDED_USE,
        }
        try:
            response = await self._request("GET", self._url(), params={"restype": "container"})
        except OMFError as exc:
            return StorageStatus(**base, status=HealthStatus.DOWN, message=exc.message)
        if response.status_code >= 400:
            message = (
                f"container {self._config.container!r} not found"
                if response.status_code == 404
                else self._error(response).message
            )
            return StorageStatus(**base, status=HealthStatus.DOWN, message=message)
        return StorageStatus(
            **base,
            status=HealthStatus.UP,
            details={
                "account": self._config.account_name,
                "container": self._config.container,
                "prefix": self._config.prefix,
                "endpoint": self._config.endpoint,
                "auth": "shared_key" if self._account_key else ("sas" if self._sas else "entra_id"),
            },
        )
