"""Model registry: catalog definitions, deployments and capability verification."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status

from omf.core.domain.capabilities import Capability, CapabilitySet
from omf.core.domain.models import ModelDefinition, ModelDeployment, ModelLicense
from omf_api.deps import ContainerDep
from omf_api.schemas import Envelope, ModelCreate, ModelDetail, OperationResult, VerifyRequest, ok
from omf_api.security import AdminDep, UserDep

router = APIRouter(prefix="/api/v1", tags=["models"])


@router.get("/models", summary="Model catalog (definitions independent of runtime)")
async def list_models(c: ContainerDep, _p: UserDep) -> Envelope[list[ModelDefinition]]:
    return ok(await c.models.definitions())


@router.post("/models", status_code=status.HTTP_201_CREATED, summary="Register a model definition")
async def register_model(
    body: ModelCreate, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[ModelDefinition]:
    model = ModelDefinition(
        **body.model_dump(exclude={"capabilities", "license"}),
        license=ModelLicense.model_validate(body.license) if body.license else None,
        capabilities=CapabilitySet.declared(body.capabilities, "api"),
    )
    saved = await c.models.register(model)
    await c.audit.record(
        actor=p.name, action="model.register", target=model.id, request_id=request.state.request_id
    )
    return ok(saved)


@router.get("/deployments", summary="Models as served by providers, with capability evidence")
async def list_deployments(
    c: ContainerDep,
    _p: UserDep,
    provider_id: str | None = None,
    capability: Capability | None = None,
    refresh: bool = False,
) -> Envelope[list[ModelDeployment]]:
    deployments = await c.models.deployments(provider_id, refresh=refresh)
    if capability is not None:
        deployments = [d for d in deployments if d.capabilities.supports(capability)]
    return ok(deployments)


@router.post("/deployments/verify", summary="Probe a deployment and record verified capabilities")
async def verify_deployment(
    body: VerifyRequest, c: ContainerDep, p: UserDep, request: Request
) -> Envelope[dict[str, Any]]:
    result = await c.models.verify(body.provider_id, body.model)
    await c.audit.record(
        actor=p.name,
        action="deployment.verify",
        target=f"{body.provider_id}/{body.model}",
        request_id=request.state.request_id,
        details={"checks": [ch["capability"] + ":" + ch["result"] for ch in result["checks"]]},
    )
    return ok(result)


@router.get("/models/{model_id:path}", summary="Model definition with its deployments")
async def get_model(model_id: str, c: ContainerDep, _p: UserDep) -> Envelope[ModelDetail]:
    definition = await c.models.definition(model_id)
    deployments = [d for d in await c.models.deployments() if d.model_id == definition.id]
    return ok(ModelDetail(definition=definition, deployments=deployments))


@router.delete("/models/{model_id:path}")
async def delete_model(
    model_id: str, c: ContainerDep, p: AdminDep, request: Request
) -> Envelope[OperationResult]:
    await c.models.delete(model_id)
    await c.audit.record(
        actor=p.name, action="model.delete", target=model_id, request_id=request.state.request_id
    )
    return ok(OperationResult(ok=True))
