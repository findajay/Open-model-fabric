"""Provider management: list, add, test, enable/disable, health and capabilities."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status

from omf.bootstrap.container import Container
from omf.core.domain.errors import ValidationFailed
from omf.core.domain.models import ModelDeployment, ProviderHealth
from omf.core.ports.secrets import SecretReferenceInfo
from omf_api.deps import ContainerDep
from omf_api.schemas import (
    Envelope,
    OperationResult,
    ProviderCreate,
    ProviderMutationResult,
    ProviderTestRequest,
    ProviderUpdate,
    ProviderView,
    ok,
)
from omf_api.security import AdminDep, UserDep

router = APIRouter(prefix="/api/v1", tags=["providers"])


def provider_view(c: Container, provider_id: str) -> ProviderView:
    eff = c.providers.effective(provider_id)
    d = eff.definition
    refs = []
    for field in eff.descriptor.secret_fields:
        ref = d.config.get(field)
        if ref:
            refs.append(
                SecretReferenceInfo(reference=str(ref), available=c.secrets.is_available(str(ref)))
            )
    return ProviderView(
        id=d.id,
        adapter_type=d.adapter_type,
        display_name=d.display_name,
        enabled=d.enabled,
        origin=d.origin,
        locality=eff.locality,
        allow_sensitive=eff.allow_sensitive,
        compute_environment_id=d.compute_environment_id,
        timeouts=d.timeouts,
        models=d.models,
        config=d.config,
        secret_references=refs,
        capabilities=c.providers.capabilities(provider_id),
        health=c.health.get(provider_id),
        is_default=c.settings.current.default_provider_id == provider_id,
    )


@router.get("/adapters", summary="Adapter types compiled into this build")
async def list_adapters(c: ContainerDep, _p: UserDep) -> Envelope[list[dict[str, Any]]]:
    from omf.bootstrap.catalog import describe_component_adapters

    return ok([d.describe() for d in c.catalog.all()] + describe_component_adapters())


@router.get("/providers", summary="Configured inference providers")
async def list_providers(c: ContainerDep, _p: UserDep) -> Envelope[list[ProviderView]]:
    return ok([provider_view(c, d.id) for d in c.providers.definitions()])


@router.post("/providers", status_code=status.HTTP_201_CREATED, summary="Add a runtime provider")
async def create_provider(
    body: ProviderCreate, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[ProviderMutationResult]:
    warnings = await c.providers.create(body.to_domain())
    c.models.invalidate(body.id)
    await c.audit.record(
        actor=p.name,
        action="provider.create",
        target=body.id,
        request_id=request.state.request_id,
        details={"adapter_type": body.adapter_type, "enabled": body.enabled},
    )
    if body.enabled:
        await c.health.check(body.id)
    return ok(ProviderMutationResult(provider=provider_view(c, body.id), warnings=warnings))


@router.post("/providers/test", summary="Test connectivity of a saved or unsaved provider")
async def test_provider(
    body: ProviderTestRequest, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[dict[str, Any]]:
    if body.provider_id is not None and body.definition is None:
        definition = c.providers.get(body.provider_id)
    elif body.definition is not None and body.provider_id is None:
        definition = body.definition.to_domain()
    else:
        raise ValidationFailed("provide exactly one of provider_id or definition")
    result = await c.providers.test(definition)
    await c.audit.record(
        actor=p.name,
        action="provider.test",
        target=definition.id,
        request_id=request.state.request_id,
        details={"ok": result["ok"]},
    )
    return ok(result)


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str, c: ContainerDep, _p: UserDep) -> Envelope[ProviderView]:
    return ok(provider_view(c, provider_id))


@router.patch(
    "/providers/{provider_id}", summary="Update a provider (config-origin: enable/disable only)"
)
async def update_provider(
    provider_id: str, body: ProviderUpdate, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[ProviderMutationResult]:
    changes = body.model_dump(exclude_unset=True)
    warnings = await c.providers.update(provider_id, changes)
    c.models.invalidate(provider_id)
    await c.audit.record(
        actor=p.name,
        action="provider.update",
        target=provider_id,
        request_id=request.state.request_id,
        details={"fields": sorted(changes)},
    )
    if c.providers.get(provider_id).enabled:
        await c.health.check(provider_id)
    return ok(ProviderMutationResult(provider=provider_view(c, provider_id), warnings=warnings))


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[OperationResult]:
    await c.providers.delete(provider_id)
    c.models.invalidate(provider_id)
    await c.audit.record(
        actor=p.name,
        action="provider.delete",
        target=provider_id,
        request_id=request.state.request_id,
    )
    return ok(OperationResult(ok=True))


@router.post("/providers/{provider_id}/health", summary="Run a health check now")
async def check_provider_health(
    provider_id: str, c: ContainerDep, _p: UserDep
) -> Envelope[ProviderHealth]:
    return ok(await c.health.check(provider_id))


@router.get("/providers/{provider_id}/models", summary="Deployments served by this provider")
async def provider_models(
    provider_id: str, c: ContainerDep, _p: UserDep, refresh: bool = False
) -> Envelope[list[ModelDeployment]]:
    c.providers.get(provider_id)
    return ok(await c.models.deployments(provider_id, refresh=refresh))
