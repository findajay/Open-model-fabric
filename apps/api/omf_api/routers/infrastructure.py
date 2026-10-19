"""Compute environments and artifact storage."""

from __future__ import annotations

import os
import tempfile
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from omf.core.domain.errors import ValidationFailed
from omf.core.domain.models import (
    ComputeEnvironment,
    ComputeOperation,
    StorageArtifact,
    StorageStatus,
)
from omf.core.ports.storage import validate_key
from omf_api.deps import ContainerDep
from omf_api.schemas import Envelope, OperationResult, ok
from omf_api.security import AdminDep, UserDep

router = APIRouter(prefix="/api/v1", tags=["infrastructure"])
KeyParam = Annotated[str, Query(min_length=1, max_length=1024)]


# ---------------------------------------------------------------------- compute


@router.get("/compute", summary="Compute environments")
async def list_compute(
    c: ContainerDep, _p: UserDep, refresh: bool = False
) -> Envelope[list[ComputeEnvironment]]:
    return ok(await c.compute.describe_all(refresh=refresh))


@router.get("/compute/{environment_id}")
async def get_compute(
    environment_id: str, c: ContainerDep, _p: UserDep, refresh: bool = True
) -> Envelope[ComputeEnvironment]:
    return ok(await c.compute.describe(environment_id, refresh=refresh))


@router.post(
    "/compute/{environment_id}/{operation}",
    summary="Lifecycle operation (admin). 501 if the adapter cannot, 403 lifecycle_disabled "
    "unless the environment allows it",
)
async def compute_operation(
    environment_id: str,
    operation: Literal["provision", "start", "stop", "deprovision"],
    c: ContainerDep,
    p: AdminDep,
    request: Request,
    wait: bool = False,
    timeout_s: float = Query(default=300.0, gt=0, le=3600),
) -> Envelope[ComputeEnvironment]:
    c.compute.provider(environment_id)  # 404 before auditing unknown ids
    await c.audit.record(
        actor=p.name,
        action=f"compute.{operation}",
        target=environment_id,
        request_id=request.state.request_id,
        details={"wait": wait},
    )
    env = await c.compute.operate(
        environment_id, ComputeOperation(operation), wait=wait, timeout_s=timeout_s
    )
    return ok(env)


# ---------------------------------------------------------------------- storage


@router.get("/storage", summary="Storage providers and their status")
async def list_storage(c: ContainerDep, _p: UserDep) -> Envelope[list[StorageStatus]]:
    return ok(await c.storage.statuses())


@router.get("/storage/{storage_id}/artifacts")
async def list_artifacts(
    storage_id: str, c: ContainerDep, _p: UserDep, prefix: str = ""
) -> Envelope[list[StorageArtifact]]:
    return ok(await c.storage.get(storage_id).list(prefix))


@router.post(
    "/storage/{storage_id}/artifacts",
    status_code=status.HTTP_201_CREATED,
    summary="Upload an artifact (validated key, size and content type)",
)
async def upload_artifact(
    storage_id: str,
    c: ContainerDep,
    p: AdminDep,
    request: Request,
    key: Annotated[str, Form(min_length=1, max_length=1024)],
    file: Annotated[UploadFile, File()],
) -> Envelope[StorageArtifact]:
    validate_key(key)
    content_type = (file.content_type or "application/octet-stream").split(";")[0].strip()
    allowed = c.config.security.allowed_upload_content_types
    if content_type not in allowed:
        raise ValidationFailed(
            f"content type {content_type!r} is not allowed", details={"allowed": allowed}
        )
    if file.size is not None and file.size > c.config.server.max_upload_bytes:
        raise ValidationFailed(
            "file too large", details={"limit": c.config.server.max_upload_bytes}
        )
    artifact = await c.storage.get(storage_id).put(
        key, file.file, content_type=content_type, metadata={"uploaded_by": p.name}
    )
    await c.audit.record(
        actor=p.name,
        action="artifact.put",
        target=f"{storage_id}:{key}",
        request_id=request.state.request_id,
        details={"size": artifact.size_bytes, "sha256": artifact.sha256},
    )
    return ok(artifact)


@router.get("/storage/{storage_id}/artifact", summary="Artifact metadata")
async def artifact_metadata(
    storage_id: str, key: KeyParam, c: ContainerDep, _p: UserDep
) -> Envelope[StorageArtifact]:
    return ok(await c.storage.get(storage_id).get_metadata(validate_key(key)))


@router.get(
    "/storage/{storage_id}/artifact/content",
    summary="Download an artifact (checksum-verified)",
    response_class=FileResponse,
)
async def artifact_content(
    storage_id: str, key: KeyParam, c: ContainerDep, _p: UserDep
) -> FileResponse:
    fd, path = tempfile.mkstemp(prefix="omf-dl-")
    try:
        with os.fdopen(fd, "wb") as fh:
            artifact = await c.storage.get(storage_id).get(validate_key(key), fh)
    except BaseException:
        os.unlink(path)
        raise
    filename = key.rsplit("/", 1)[-1]
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"X-Content-SHA256": artifact.sha256},
        background=BackgroundTask(os.unlink, path),
    )


@router.post("/storage/{storage_id}/artifact/verify", summary="Re-hash stored content")
async def verify_artifact(
    storage_id: str, key: KeyParam, c: ContainerDep, _p: UserDep
) -> Envelope[OperationResult]:
    valid = await c.storage.get(storage_id).verify_checksum(validate_key(key))
    return ok(
        OperationResult(ok=valid, message="checksum matches" if valid else "checksum mismatch")
    )


@router.delete("/storage/{storage_id}/artifact")
async def delete_artifact(
    storage_id: str, key: KeyParam, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[OperationResult]:
    await c.storage.get(storage_id).delete(validate_key(key))
    await c.audit.record(
        actor=p.name,
        action="artifact.delete",
        target=f"{storage_id}:{key}",
        request_id=request.state.request_id,
    )
    return ok(OperationResult(ok=True))
