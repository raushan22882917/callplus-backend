import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ToolContext:
    """Authorization context for a tool call made by an AI agent during a
    conversation — distinct from RequestContext (apps/api/app/core/deps.py),
    which is for human, JWT-authenticated requests.

    tenant_id here comes from the Call/Conversation row the turn belongs to,
    which itself was only ever set from a verified source: the phone_numbers
    lookup on an inbound webhook, or the tenant-scoped RequestContext that
    created an outbound call. It never comes from anything the model or the
    realtime gateway can inject directly.

    What a tool is allowed to do is gated by ai_agent_tools (which tools are
    enabled for this agent), not by human RBAC roles/permissions.

    Also reused by the workflow engine (app/workflows/engine.py) to run
    tool handlers as workflow actions — agent_id is None in that case since
    no AI agent/LLM is involved.
    """

    db: AsyncSession
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None
    call_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    # True only for the staff assistant. Staff-only tools refuse every other caller.
    staff: bool = False
