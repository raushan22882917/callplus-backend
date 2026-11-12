import uuid

from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConversationRead,
)
from app.schemas.conversation import MessageRead
from app.services.agent_engine import run_assistant_turn
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Conversation, ConversationChannel, Message

router = APIRouter(prefix="/assistant", tags=["assistant"])


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation


async def _get_own_conversation(
    repo: ConversationRepository, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    conversation = await repo.get(conversation_id)
    if conversation.channel != ConversationChannel.INTERNAL or (
        conversation.created_by_user_id != user_id
    ):
        raise NotFoundError("Conversation not found")
    return conversation


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    payload: AssistantChatRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.ASSISTANT_USE)),
    db: AsyncSession = Depends(get_db),
) -> AssistantChatResponse:
    repo = ConversationRepository(db, ctx.tenant_id)
    if payload.conversation_id is not None:
        conversation = await _get_own_conversation(repo, payload.conversation_id, ctx.user_id)
    else:
        conversation = await repo.create(
            channel=ConversationChannel.INTERNAL, created_by_user_id=ctx.user_id
        )

    reply_text = await run_assistant_turn(
        db=db,
        tenant_id=ctx.tenant_id,
        conversation=conversation,
        user_text=payload.message,
        page=payload.page,
    )
    await db.commit()

    return AssistantChatResponse(conversation_id=conversation.id, reply_text=reply_text)


@router.get("/conversations", response_model=list[AssistantConversationRead])
async def list_conversations(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.ASSISTANT_USE)),
    db: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.tenant_id == ctx.tenant_id,
            Conversation.channel == ConversationChannel.INTERNAL,
            Conversation.created_by_user_id == ctx.user_id,
        )
        .order_by(Conversation.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_conversation_messages(
    conversation_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.ASSISTANT_USE)),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    repo = ConversationRepository(db, ctx.tenant_id)
    await _get_own_conversation(repo, conversation_id, ctx.user_id)

    result = await db.execute(
        select(Message)
        .where(Message.tenant_id == ctx.tenant_id, Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return list(result.scalars().all())
