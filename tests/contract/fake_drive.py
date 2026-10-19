"""In-memory fake of the Google Drive v3 REST endpoints used by the Drive adapter.

Implements: files.list (with the ``q`` clauses the adapter emits), resumable create/update
uploads (``Content-Range`` chunking, ``308 Resume Incomplete``, status queries), media
download, metadata get and trash. Can inject a transient 503 on an upload chunk.
This verifies the adapter's protocol handling; it is not a substitute for testing
against real Google Drive (see docs/providers/storage.md).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

FOLDER_ID = "fakeFolder0123456789"
_CLAUSE = re.compile(r"appProperties has \{ key='([^']*)' and value='([^']*)' \}")


@dataclass
class FakeDrive:
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    content: dict[str, bytes] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_next_chunk: bool = False
    requests: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        self.requests.append(f"{request.method} {path}")
        if request.headers.get("authorization") != "Bearer fake-token":
            return httpx.Response(401, json={"error": {"message": "unauthenticated"}})
        if request.method == "GET" and path == "/drive/v3/files":
            return self._list(params)
        if (
            request.method == "POST"
            and path == "/upload/drive/v3/files"
            and "upload_id" not in params
        ):
            return self._start_session(
                None, json.loads(request.content), int(request.headers["x-upload-content-length"])
            )
        if request.method == "PATCH" and path.startswith("/upload/drive/v3/files/"):
            return self._start_session(
                path.rsplit("/", 1)[1],
                json.loads(request.content),
                int(request.headers["x-upload-content-length"]),
            )
        if request.method == "PUT" and "upload_id" in params:
            return self._chunk(params["upload_id"], request)
        if request.method == "GET" and path == f"/drive/v3/files/{FOLDER_ID}":
            return httpx.Response(
                200,
                json={
                    "id": FOLDER_ID,
                    "name": "omf",
                    "trashed": False,
                    "mimeType": "application/vnd.google-apps.folder",
                    "capabilities": {"canAddChildren": True},
                },
            )
        if (
            request.method == "GET"
            and path.startswith("/drive/v3/files/")
            and params.get("alt") == "media"
        ):
            fid = path.rsplit("/", 1)[1]
            if fid not in self.content:
                return httpx.Response(404, json={"error": {"message": "File not found"}})
            return httpx.Response(200, content=self.content[fid])
        if request.method == "PATCH" and path.startswith("/drive/v3/files/"):
            fid = path.rsplit("/", 1)[1]
            if fid not in self.files:
                return httpx.Response(404, json={"error": {"message": "File not found"}})
            self.files[fid].update(json.loads(request.content))
            return httpx.Response(200, json=self.files[fid])
        return httpx.Response(
            400, json={"error": {"message": f"fake drive: unsupported {request.method} {path}"}}
        )

    def _list(self, params: dict[str, str]) -> httpx.Response:
        q = params.get("q", "")
        wanted = dict(_CLAUSE.findall(q))
        out = []
        for f in self.files.values():
            if f"'{FOLDER_ID}' in parents" in q and FOLDER_ID not in f["parents"]:
                continue
            if "trashed = false" in q and f.get("trashed"):
                continue
            props = f.get("appProperties", {})
            if all(props.get(k) == v for k, v in wanted.items()):
                out.append(f)
        return httpx.Response(200, json={"files": out})

    def _start_session(
        self, file_id: str | None, metadata: dict[str, Any], size: int
    ) -> httpx.Response:
        if file_id is not None and file_id not in self.files:
            return httpx.Response(404, json={"error": {"message": "File not found"}})
        sid = uuid.uuid4().hex
        self.sessions[sid] = {"file_id": file_id, "metadata": metadata, "size": size, "data": b""}
        return httpx.Response(
            200,
            headers={
                "Location": f"https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id={sid}"
            },
        )

    def _chunk(self, sid: str, request: httpx.Request) -> httpx.Response:
        s = self.sessions[sid]
        rng = request.headers["content-range"]
        if rng.startswith("bytes */"):  # status query or empty upload
            if s["size"] == 0 or len(s["data"]) == s["size"]:
                return self._finish(sid)
            return self._incomplete(s)
        if self.fail_next_chunk:
            self.fail_next_chunk = False
            return httpx.Response(503, json={"error": {"message": "backend error"}})
        start, end = (int(x) for x in re.match(r"bytes (\d+)-(\d+)/\d+", rng).groups())  # type: ignore[union-attr]
        assert start == len(s["data"]), "chunks must be contiguous"
        s["data"] += request.content
        assert len(s["data"]) == end + 1
        if len(s["data"]) == s["size"]:
            return self._finish(sid)
        return self._incomplete(s)

    @staticmethod
    def _incomplete(s: dict[str, Any]) -> httpx.Response:
        headers = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return httpx.Response(308, headers=headers)

    def _finish(self, sid: str) -> httpx.Response:
        s = self.sessions.pop(sid)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        fid = s["file_id"] or uuid.uuid4().hex
        f = self.files.get(
            fid, {"id": fid, "createdTime": now, "parents": s["metadata"].get("parents", [])}
        )
        f.update(
            {
                "name": s["metadata"].get("name", f.get("name")),
                "mimeType": s["metadata"].get("mimeType"),
                "appProperties": s["metadata"].get("appProperties", {}),
                "size": str(len(s["data"])),
                "modifiedTime": now,
                "trashed": False,
            }
        )
        self.files[fid] = f
        self.content[fid] = s["data"]
        return httpx.Response(200, json=f)

    def corrupt(self, key: str) -> None:
        for fid, f in self.files.items():
            if f["name"] == key:
                self.content[fid] = b"tampered" + self.content[fid]
