"""Conversation history."""

from __future__ import annotations

from fastapi import APIRouter, status

from omf.core.domain.errors import ConversationNotFound
from omf.core.domain.models import Conversation
from omf_api.deps import ContainerDep
from omf_api.schemas import ConversationCreate, ConversationUpdate, Envelope, OperationResult, ok
from omf_api.security import UserDep

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(
    c: ContainerDep, _p: UserDep, limit: int = 50
) -> Envelope[list[Conversation]]:
    return ok(await c.metadata.list_conversations(limit=max(1, min(limit, 200))))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate, c: ContainerDep, _p: UserDep
) -> Envelope[Conversation]:
    conversation = Conversation(title=body.title)
    await c.metadata.create_conversation(conversation)
    return ok(conversation)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str, c: ContainerDep, _p: UserDep
) -> Envelope[Conversation]:
    conversation = await c.metadata.get_conversation(conversation_id)
    if conversation is None:
        raise ConversationNotFound(f"conversation {conversation_id!r} not found")
    return ok(conversation)


@router.patch("/{conversation_id}")
async def rename_conversation(
    conversation_id: str, body: ConversationUpdate, c: ContainerDep, _p: UserDep
) -> Envelope[OperationResult]:
    await c.metadata.rename_conversation(conversation_id, body.title)
    return ok(OperationResult(ok=True))


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, c: ContainerDep, _p: UserDep
) -> Envelope[OperationResult]:
    if not await c.metadata.delete_conversation(conversation_id):
        raise ConversationNotFound(f"conversation {conversation_id!r} not found")
    return ok(OperationResult(ok=True))
