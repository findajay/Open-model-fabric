"""Health probes, system info, settings, metrics, audit."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from omf import __version__
from omf.core.domain.models import AuditEntry, InferenceRecord, ProviderHealth, RequestStatus
from omf.core.ports.secrets import SecretReferenceInfo
from omf_api.deps import ContainerDep
from omf_api.schemas import Envelope, MetricsSummary, ProviderStats, SystemInfo, ok
from omf_api.security import AdminDep, UserDep

health_router = APIRouter(tags=["health"])
router = APIRouter(prefix="/api/v1", tags=["system"])


@health_router.get("/health/live", summary="Liveness: the process is running")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get(
    "/health/ready", summary="Readiness: metadata store reachable, providers registered"
)
async def ready(c: ContainerDep) -> JSONResponse:
    db_ok = await c.metadata.ping()
    providers = c.health.all()
    enabled = [h for h in providers if h.status.value != "disabled"]
    body: dict[str, Any] = {
        "status": "ready" if db_ok and enabled else "not_ready",
        "checks": {
            "metadata_store": "ok" if db_ok else "unreachable",
            "enabled_providers": len(enabled),
            # Provider outages degrade functionality but do not make the API unready.
            "providers": {h.provider_id: h.status.value for h in providers},
        },
    }
    return JSONResponse(status_code=200 if body["status"] == "ready" else 503, content=body)


@router.get("/system", summary="Version, profile and the calling principal")
async def system_info(c: ContainerDep, p: UserDep) -> Envelope[SystemInfo]:
    return ok(
        SystemInfo(
            version=__version__,
            profile=c.config.profile,
            environment=c.config.environment,
            auth_mode=c.config.auth.mode,
            principal=p.name,
            role=p.role,
            adapters=[d.adapter_type for d in c.catalog.all()],
        )
    )


@router.get("/health/providers", summary="Latest provider health")
async def provider_health(
    c: ContainerDep, _p: UserDep, refresh: bool = False
) -> Envelope[list[ProviderHealth]]:
    if refresh:
        await c.health.check_all()
    return ok(c.health.all())


@router.get("/settings")
async def get_settings(c: ContainerDep, _p: UserDep) -> Envelope[dict[str, Any]]:
    return ok(
        {"runtime": c.settings.current.model_dump(mode="json"), "read_only": c.settings.read_only()}
    )


@router.patch("/settings", summary="Update runtime settings (validated, persisted, audited)")
async def update_settings(
    body: dict[str, Any], c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[dict[str, Any]]:
    updated = await c.settings.update(body)
    await c.audit.record(
        actor=p.name,
        action="settings.update",
        target="runtime_settings",
        request_id=request.state.request_id,
        details={"fields": sorted(body)},
    )
    return ok({"runtime": updated.model_dump(mode="json"), "read_only": c.settings.read_only()})


@router.get("/requests/recent", summary="Recent inference requests (no prompt content)")
async def recent_requests(
    c: ContainerDep, _p: UserDep, limit: int = 50
) -> Envelope[list[InferenceRecord]]:
    return ok(await c.metadata.recent_records(limit=max(1, min(limit, 500))))


@router.get("/metrics/summary", summary="Per-provider latency and error statistics")
async def metrics_summary(
    c: ContainerDep,
    _p: UserDep,
    window: int = 500,
    operation: Literal["chat", "embeddings"] = "chat",
) -> Envelope[MetricsSummary]:
    """Statistics over the last ``window`` records of one operation (chat by default, so
    embedding latencies do not distort chat latency percentiles)."""
    records = [
        r
        for r in await c.metadata.recent_records(limit=max(1, min(window, 5000)))
        if r.operation == operation
    ]
    by_provider: dict[str, list[InferenceRecord]] = defaultdict(list)
    for r in records:
        by_provider[r.provider_id or "unrouted"].append(r)

    def pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        return round(values[min(len(values) - 1, round(q * (len(values) - 1)))], 2)

    stats = []
    for pid, rs in sorted(by_provider.items()):
        done = [r for r in rs if r.status == RequestStatus.COMPLETED]
        errors = [r for r in rs if r.status == RequestStatus.FAILED]
        lat = [r.latency_ms for r in done if r.latency_ms is not None]
        ttft = [r.ttft_ms for r in done if r.ttft_ms is not None]
        tps = [r.tokens_per_second for r in done if r.tokens_per_second is not None]
        stats.append(
            ProviderStats(
                provider_id=pid,
                requests=len(rs),
                errors=len(errors),
                error_rate=round(len(errors) / len(rs), 4) if rs else 0.0,
                p50_latency_ms=pct(lat, 0.5),
                p95_latency_ms=pct(lat, 0.95),
                mean_ttft_ms=round(statistics.fmean(ttft), 2) if ttft else None,
                mean_tokens_per_second=round(statistics.fmean(tps), 2) if tps else None,
            )
        )
    return ok(MetricsSummary(window_requests=len(records), providers=stats))


@router.get("/metrics/telemetry", summary="In-process telemetry aggregates")
async def telemetry_snapshot(c: ContainerDep, _p: UserDep) -> Envelope[dict[str, Any]]:
    return ok(c.logging_telemetry.snapshot())


@router.get("/audit", summary="Audit log (admin)")
async def audit_log(c: ContainerDep, _p: AdminDep, limit: int = 100) -> Envelope[list[AuditEntry]]:
    return ok(await c.audit.list(max(1, min(limit, 1000))))


@router.get("/secrets", summary="Secret references and availability (never values; admin)")
async def secret_references(c: ContainerDep, _p: AdminDep) -> Envelope[list[SecretReferenceInfo]]:
    refs = set(c.config.secret_references())
    for definition in c.providers.definitions():  # includes providers added at runtime
        descriptor = c.providers.effective(definition.id).descriptor
        refs.update(
            str(definition.config[f]) for f in descriptor.secret_fields if definition.config.get(f)
        )
    return ok(
        [
            SecretReferenceInfo(reference=r, available=c.secrets.is_available(r))
            for r in sorted(refs)
        ]
    )
