"""Text embeddings."""

from __future__ import annotations

from fastapi import APIRouter, Request

from omf.core.domain.models import EmbeddingResponse
from omf.core.services.inference import EmbedCommand
from omf_api.deps import ContainerDep
from omf_api.schemas import EmbeddingsRequest, Envelope, ok
from omf_api.security import UserDep

router = APIRouter(prefix="/api/v1", tags=["embeddings"])


@router.post(
    "/embeddings",
    summary="Embed texts with a model that declares the embeddings capability",
)
async def create_embeddings(
    body: EmbeddingsRequest, request: Request, c: ContainerDep, _p: UserDep
) -> Envelope[EmbeddingResponse]:
    return ok(
        await c.inference.embed(
            EmbedCommand(
                inputs=body.inputs(),
                provider_id=body.provider_id,
                model=body.model,
                dimensions=body.dimensions,
                sensitivity=body.sensitivity,
                request_id=request.state.request_id,
            )
        )
    )
