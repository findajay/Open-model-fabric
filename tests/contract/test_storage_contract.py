"""Acceptance criterion 10: every storage adapter passes the same contract tests."""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from omf.adapters.storage.google_drive import GoogleDriveConfig, GoogleDriveStorageAdapter
from omf.adapters.storage.local_fs import LocalFilesystemConfig, LocalFilesystemStorageAdapter
from omf.core.domain.errors import ArtifactNotFound, ChecksumMismatch, ValidationFailed
from omf.core.domain.models import HealthStatus
from omf.core.ports.storage import ArtifactStorageProvider
from tests.contract.fake_drive import FOLDER_ID, FakeDrive


@dataclass
class Backend:
    adapter: ArtifactStorageProvider
    corrupt: Callable[[str], None]


async def _token() -> str:
    return "fake-token"


@pytest.fixture(params=["local_fs", "google_drive", "s3"])
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Backend]:
    kind = request.param
    if kind == "local_fs":
        adapter = LocalFilesystemStorageAdapter(
            "local", LocalFilesystemConfig(root=str(tmp_path), keep_versions=2)
        )

        def corrupt(key: str) -> None:
            h = hashlib.sha256(key.encode()).hexdigest()
            blob = sorted((tmp_path / "objects" / h[:2] / h).glob("v*.bin"))[-1]
            blob.write_bytes(b"tampered" + blob.read_bytes())

        yield Backend(adapter, corrupt)
    elif kind == "google_drive":
        drive = FakeDrive()
        adapter = GoogleDriveStorageAdapter(
            "gdrive",
            GoogleDriveConfig(folder_id=FOLDER_ID, credentials_ref="env:UNUSED", chunk_size_mb=1),
            _token,
            transport=drive.transport(),
        )
        adapter.fake = drive  # type: ignore[attr-defined]
        yield Backend(adapter, drive.corrupt)
        await adapter.aclose()
    else:
        moto = pytest.importorskip("moto")
        import boto3

        from omf.adapters.storage.s3 import S3Config, S3StorageAdapter

        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        with moto.mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="omf-test")
            adapter = S3StorageAdapter("s3", S3Config(bucket="omf-test", prefix="omf/"), client)

            def corrupt(key: str) -> None:
                obj = client.get_object(Bucket="omf-test", Key=f"omf/{key}")
                client.put_object(
                    Bucket="omf-test", Key=f"omf/{key}", Body=b"tampered", Metadata=obj["Metadata"]
                )

            yield Backend(adapter, corrupt)


async def put_bytes(s: ArtifactStorageProvider, key: str, data: bytes, **kw: object):  # type: ignore[no-untyped-def]
    return await s.put(key, io.BytesIO(data), **kw)  # type: ignore[arg-type]


async def get_bytes(s: ArtifactStorageProvider, key: str) -> bytes:
    buf = io.BytesIO()
    await s.get(key, buf)
    return buf.getvalue()


async def test_roundtrip_and_checksum(backend: Backend) -> None:
    s = backend.adapter
    data = b"model weights \x00\x01\x02" * 100
    artifact = await put_bytes(
        s,
        "models/m1/weights.bin",
        data,
        content_type="application/octet-stream",
        metadata={"source": "test"},
    )
    assert artifact.sha256 == hashlib.sha256(data).hexdigest()
    assert artifact.size_bytes == len(data)
    assert artifact.key == "models/m1/weights.bin"
    assert await get_bytes(s, "models/m1/weights.bin") == data
    meta = await s.get_metadata("models/m1/weights.bin")
    assert meta.metadata == {"source": "test"}
    assert meta.sha256 == artifact.sha256


async def test_versions_increment_and_latest_wins(backend: Backend) -> None:
    s = backend.adapter
    v1 = await put_bytes(s, "experiments/run.json", b'{"v": 1}', content_type="application/json")
    v2 = await put_bytes(s, "experiments/run.json", b'{"v": 2}', content_type="application/json")
    assert int(v2.version) == int(v1.version) + 1
    assert await get_bytes(s, "experiments/run.json") == b'{"v": 2}'
    assert len(await s.list("experiments/")) == 1


async def test_list_with_prefix(backend: Backend) -> None:
    s = backend.adapter
    for key in ["b/2.txt", "a/1.txt", "a/sub/3.txt", "c.txt"]:
        await put_bytes(s, key, key.encode(), content_type="text/plain")
    assert [a.key for a in await s.list("a/")] == ["a/1.txt", "a/sub/3.txt"]
    assert [a.key for a in await s.list()] == ["a/1.txt", "a/sub/3.txt", "b/2.txt", "c.txt"]
    assert await s.list("zzz/") == []


async def test_delete(backend: Backend) -> None:
    s = backend.adapter
    await put_bytes(s, "tmp/x.bin", b"x")
    await s.delete("tmp/x.bin")
    with pytest.raises(ArtifactNotFound):
        await get_bytes(s, "tmp/x.bin")
    with pytest.raises(ArtifactNotFound):
        await s.delete("tmp/x.bin")
    with pytest.raises(ArtifactNotFound):
        await s.get_metadata("never/existed")


async def test_verify_checksum_and_corruption_detection(backend: Backend) -> None:
    s = backend.adapter
    data = b"important artifact"
    await put_bytes(s, "models/a.gguf", data)
    assert await s.verify_checksum("models/a.gguf")
    assert await s.verify_checksum("models/a.gguf", hashlib.sha256(data).hexdigest())
    assert not await s.verify_checksum("models/a.gguf", "0" * 64)
    backend.corrupt("models/a.gguf")
    assert not await s.verify_checksum("models/a.gguf")
    with pytest.raises(ChecksumMismatch):
        await get_bytes(s, "models/a.gguf")


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "/abs/path",
        "a//b",
        "a/./b",
        "a/../b",
        "",
        "a\\b",
        "with space/x",
        "a/" + "x" * 300,
    ],
)
async def test_unsafe_keys_rejected(backend: Backend, key: str) -> None:
    with pytest.raises(ValidationFailed):
        await put_bytes(backend.adapter, key, b"x")


async def test_large_streamed_file(backend: Backend) -> None:
    s = backend.adapter
    data = os.urandom(2 * 1024 * 1024 + 12345)  # > 2 upload chunks for Drive
    if hasattr(s, "fake"):
        s.fake.fail_next_chunk = True  # exercise resumable-upload recovery
    artifact = await put_bytes(s, "models/big.safetensors", data)
    assert artifact.size_bytes == len(data)
    assert await get_bytes(s, "models/big.safetensors") == data
    if hasattr(s, "fake"):
        assert s.fake.fail_next_chunk is False  # the injected 503 was hit and recovered from
        assert sum(r.startswith("PUT") for r in s.fake.requests) >= 4  # 3 chunks + status query


async def test_empty_file(backend: Backend) -> None:
    s = backend.adapter
    artifact = await put_bytes(s, "empty.txt", b"", content_type="text/plain")
    assert artifact.size_bytes == 0
    assert await get_bytes(s, "empty.txt") == b""


async def test_status(backend: Backend) -> None:
    status = await backend.adapter.status()
    assert status.status == HealthStatus.UP
    assert status.intended_use
