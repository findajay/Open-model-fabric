"""In-memory fake of the Azure Blob REST endpoints the ``azure_blob`` adapter uses.

Covers Put Blob, Put Block, Put Block List, Get Blob, Get Blob Properties, Delete Blob,
List Blobs (with metadata and paging) and Get Container Properties. It checks the shape
of authentication (a ``SharedKey account:signature`` or ``Bearer`` header, or SAS query
parameters) but does NOT verify Shared Key signatures. That is what the Azurite run of
the contract suite is for, since Azurite validates them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

ACCOUNT = "devstoreaccount1"
CONTAINER = "omf-test"


@dataclass
class Blob:
    data: bytes
    content_type: str
    metadata: dict[str, str]
    modified: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class FakeAzureBlob:
    blobs: dict[str, Blob] = field(default_factory=dict)
    blocks: dict[tuple[str, str], bytes] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    page_size: int = 2
    auth: str = "shared_key"  # shared_key | sas | bearer

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def corrupt(self, key: str) -> None:
        blob = self.blobs[f"omf/{key}"]
        blob.data = b"tampered" + blob.data

    def _authorized(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if self.auth == "shared_key":
            return header.startswith(f"SharedKey {ACCOUNT}:") and len(header) > 30
        if self.auth == "bearer":
            return header == "Bearer storage-token"
        return request.url.params.get("sig") == "fake-signature" and not header

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if not self._authorized(request):
            return httpx.Response(403, headers={"x-ms-error-code": "AuthenticationFailed"})
        if not request.headers.get("x-ms-version"):
            return httpx.Response(400, headers={"x-ms-error-code": "MissingRequiredHeader"})
        prefix = f"/{ACCOUNT}/{CONTAINER}"
        path = request.url.path
        if not path.startswith(prefix):
            return httpx.Response(404, headers={"x-ms-error-code": "ContainerNotFound"})
        name = unquote(path[len(prefix) + 1 :])
        params = request.url.params
        if not name:
            if params.get("comp") == "list":
                return self._list(params.get("prefix", ""), params.get("marker", ""))
            return httpx.Response(200)  # container properties
        method = request.method
        if method == "PUT" and params.get("comp") == "block":
            self.blocks[(name, params["blockid"])] = request.content
            return httpx.Response(201)
        if method == "PUT" and params.get("comp") == "blocklist":
            ids = [el.text or "" for el in ET.fromstring(request.content)]
            data = b"".join(self.blocks.pop((name, i)) for i in ids)
            self._store(name, data, request, request.headers.get("x-ms-blob-content-type", ""))
            return httpx.Response(201)
        if method == "PUT":
            if request.headers.get("x-ms-blob-type") != "BlockBlob":
                return httpx.Response(400, headers={"x-ms-error-code": "MissingRequiredHeader"})
            self._store(name, request.content, request, request.headers.get("content-type", ""))
            return httpx.Response(201)
        blob = self.blobs.get(name)
        if blob is None:
            return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
        headers = {
            "content-length": str(len(blob.data)),
            "content-type": blob.content_type,
            "last-modified": format_datetime(blob.modified, usegmt=True),
            **{f"x-ms-meta-{k}": v for k, v in blob.metadata.items()},
        }
        if method == "HEAD":
            return httpx.Response(200, headers=headers)
        if method == "GET":
            return httpx.Response(200, headers=headers, content=blob.data)
        if method == "DELETE":
            del self.blobs[name]
            return httpx.Response(202)
        return httpx.Response(405)

    def _store(self, name: str, data: bytes, request: httpx.Request, content_type: str) -> None:
        metadata = {
            k[10:]: v for k, v in request.headers.items() if k.lower().startswith("x-ms-meta-")
        }
        for meta_name in metadata:
            if "-" in meta_name:  # Azure metadata names must be C# identifiers
                raise AssertionError(f"invalid metadata name {meta_name!r}")
        self.blobs[name] = Blob(data, content_type or "application/octet-stream", metadata)

    def _list(self, prefix: str, marker: str) -> httpx.Response:
        names = sorted(n for n in self.blobs if n.startswith(prefix))
        start = names.index(marker) if marker in names else 0
        page, rest = names[start : start + self.page_size], names[start + self.page_size :]
        items = []
        for n in page:
            blob = self.blobs[n]
            meta = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in blob.metadata.items())
            items.append(
                f"<Blob><Name>{escape(n)}</Name><Properties>"
                f"<Last-Modified>{format_datetime(blob.modified, usegmt=True)}</Last-Modified>"
                f"<Content-Length>{len(blob.data)}</Content-Length>"
                f"<Content-Type>{escape(blob.content_type)}</Content-Type>"
                f"</Properties><Metadata>{meta}</Metadata></Blob>"
            )
        body = (
            '<?xml version="1.0" encoding="utf-8"?><EnumerationResults>'
            f"<Blobs>{''.join(items)}</Blobs>"
            f"<NextMarker>{escape(rest[0]) if rest else ''}</NextMarker></EnumerationResults>"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/xml"}
        )
