"""Benchmark runs and exports."""

from __future__ import annotations

import io
from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import Response

from omf.core.domain.models import BenchmarkRun, StorageArtifact
from omf.core.services.benchmarks import BenchmarkDataset, BenchmarkRequest, export_csv, export_json
from omf_api.deps import ContainerDep
from omf_api.schemas import Envelope, OperationResult, ok
from omf_api.security import AdminDep, UserDep

router = APIRouter(prefix="/api/v1/benchmarks", tags=["benchmarks"])


@router.get("/datasets", summary="Versioned prompt datasets")
async def datasets(c: ContainerDep, _p: UserDep) -> Envelope[list[BenchmarkDataset]]:
    return ok(c.benchmarks.datasets())


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Start a benchmark run")
async def start_benchmark(
    body: BenchmarkRequest, c: ContainerDep, p: UserDep, request: Request, wait: bool = False
) -> Envelope[BenchmarkRun]:
    run = await c.benchmarks.submit(body, wait=wait)
    await c.audit.record(
        actor=p.name,
        action="benchmark.start",
        target=run.id,
        request_id=request.state.request_id,
        details={"targets": [f"{t.provider_id}/{t.model}" for t in body.targets]},
    )
    return ok(run)


@router.get("")
async def list_benchmarks(
    c: ContainerDep, _p: UserDep, limit: int = 50
) -> Envelope[list[BenchmarkRun]]:
    return ok(await c.benchmarks.list(max(1, min(limit, 200))))


@router.get("/{run_id}")
async def get_benchmark(run_id: str, c: ContainerDep, _p: UserDep) -> Envelope[BenchmarkRun]:
    return ok(await c.benchmarks.get(run_id))


@router.post("/{run_id}/cancel")
async def cancel_benchmark(run_id: str, c: ContainerDep, _p: UserDep) -> Envelope[OperationResult]:
    return ok(OperationResult(ok=await c.benchmarks.cancel(run_id)))


@router.get(
    "/{run_id}/export",
    summary="Export a run as CSV (samples) or JSON (full run)",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
async def export_benchmark(
    run_id: str, c: ContainerDep, _p: UserDep, format: Literal["csv", "json"] = "json"
) -> Response:
    run = await c.benchmarks.get(run_id)
    body, media = (
        (export_csv(run), "text/csv") if format == "csv" else (export_json(run), "application/json")
    )
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{run.id}.{format}"'},
    )


@router.post(
    "/{run_id}/export-to-storage",
    summary="Store an export in an artifact store (e.g. Google Drive)",
)
async def export_to_storage(
    run_id: str,
    c: ContainerDep,
    p: AdminDep,
    request: Request,
    storage_id: str | None = None,
    format: Literal["csv", "json"] = "json",
) -> Envelope[StorageArtifact]:
    run = await c.benchmarks.get(run_id)
    body = export_csv(run) if format == "csv" else export_json(run)
    key = f"benchmarks/{run.id}.{format}"
    storage = c.storage.get(storage_id or c.settings.current.default_storage_id)
    artifact = await storage.put(
        key,
        io.BytesIO(body.encode()),
        content_type="text/csv" if format == "csv" else "application/json",
        metadata={"benchmark_run": run.id, "dataset": f"{run.dataset_id}@{run.dataset_version}"},
    )
    await c.audit.record(
        actor=p.name,
        action="benchmark.export",
        target=f"{storage.storage_provider_id}:{key}",
        request_id=request.state.request_id,
    )
    return ok(artifact)
