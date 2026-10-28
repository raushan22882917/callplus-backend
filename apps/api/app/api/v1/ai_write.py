"""Writing help for any text box in the dashboard ("Write with AI")."""

from typing import Literal

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.rate_limit_ip import ip_rate_limit
from app.integrations.credentials import get_sarvam_api_key
from app.services import ai_writer
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/assistant", tags=["assistant"])

Kind = Literal[
    "agent_instructions",
    "greeting",
    "reply",
    "message",
    "whatsapp_template",
    "note",
    "knowledge",
    "generic",
]


class WriteRequest(BaseModel):
    kind: Kind = "generic"
    # What the person wants done ("make it shorter", "write a reminder for a haircut booking").
    instruction: str = Field(default="", max_length=600)
    current_text: str = Field(default="", max_length=8000)
    # Facts to use, such as the shop name or the customer's message. Short strings only.
    context: dict[str, str] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def _small_context(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 12:
            raise ValueError("Too much context")
        return {str(k)[:40]: str(v)[:3000] for k, v in value.items()}


class WriteResponse(BaseModel):
    text: str


@router.post(
    "/write",
    response_model=WriteResponse,
    dependencies=[Depends(ip_rate_limit("ai_write", limit=40))],
)
async def write_text(
    payload: WriteRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.ASSISTANT_USE)),
    db: AsyncSession = Depends(get_db),
) -> WriteResponse:
    """Drafts or rewrites text. Nothing is saved; the person reads it and chooses to use it."""
    has_facts = any(v.strip() for v in payload.context.values())
    if not payload.instruction.strip() and not payload.current_text.strip() and not has_facts:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Tell the AI what to write, or type a few words first.",
        )
    api_key = await get_sarvam_api_key(db, ctx.tenant_id)
    try:
        text = await ai_writer.write(
            api_key=api_key,
            kind=payload.kind,
            instruction=payload.instruction,
            current_text=payload.current_text,
            context=payload.context,
        )
    except ai_writer.WriterError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return WriteResponse(text=text)
