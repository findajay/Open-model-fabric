"""Conversation history, scoped to the principal that created it (admins see all)."""

from __future__ import annotations

from fastapi import APIRouter, status

from omf.bootstrap.container import Container
from omf.core.domain.errors import ConversationNotFound
from omf.core.domain.models import Conversation
from omf_api.deps import ContainerDep
from omf_api.schemas import ConversationCreate, ConversationUpdate, Envelope, OperationResult, ok
from omf_api.security import Principal, UserDep

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


async def _owned(
    c: Container, conversation_id: str, p: Principal, *, messages: bool = True
) -> Conversation:
    conversation = await c.metadata.get_conversation(conversation_id, include_messages=messages)
    visible = conversation is not None and (
        p.role == "admin" or conversation.owner is None or conversation.owner == p.name
    )
    if conversation is None or not visible:
        # Identical response for "missing" and "someone else's".
        raise ConversationNotFound(f"conversation {conversation_id!r} not found")
    return conversation


@router.get("", summary="Conversations of the caller (admins: all_users=true lists everyone's)")
async def list_conversations(
    c: ContainerDep, p: UserDep, limit: int = 50, all_users: bool = False
) -> Envelope[list[Conversation]]:
    owner = None if (all_users and p.role == "admin") else p.name
    return ok(await c.metadata.list_conversations(limit=max(1, min(limit, 200)), owner=owner))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate, c: ContainerDep, p: UserDep
) -> Envelope[Conversation]:
    conversation = Conversation(title=body.title, owner=p.name)
    await c.metadata.create_conversation(conversation)
    return ok(conversation)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str, c: ContainerDep, p: UserDep
) -> Envelope[Conversation]:
    return ok(await _owned(c, conversation_id, p))


@router.patch("/{conversation_id}")
async def rename_conversation(
    conversation_id: str, body: ConversationUpdate, c: ContainerDep, p: UserDep
) -> Envelope[OperationResult]:
    await _owned(c, conversation_id, p, messages=False)
    await c.metadata.rename_conversation(conversation_id, body.title)
    return ok(OperationResult(ok=True))


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, c: ContainerDep, p: UserDep
) -> Envelope[OperationResult]:
    await _owned(c, conversation_id, p, messages=False)
    await c.metadata.delete_conversation(conversation_id)
    return ok(OperationResult(ok=True))
