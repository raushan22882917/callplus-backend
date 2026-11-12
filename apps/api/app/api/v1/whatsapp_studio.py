"""WhatsApp marketing numbers."""

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.services.whatsapp_studio import whatsapp_overview
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["whatsapp-marketing"])


class DailyWhatsApp(BaseModel):
    day: str
    sent: int
    replies: int


class WhatsAppCampaignStat(BaseModel):
    id: str
    name: str
    status: str
    scheduled_at: str | None
    created_at: str
    sent: int
    failed: int
    pending: int


class WhatsAppThread(BaseModel):
    conversation_id: str
    customer_name: str | None
    text: str
    at: str
    direction: str


class WhatsAppOverview(BaseModel):
    conversations: int
    messages_sent: int
    replies: int
    customers_with_phone: int
    new_leads: int
    qualified_leads: int
    won_inr: int
    appointments: int
    automations: int
    numbers: int
    delivery: dict[str, int]
    contacts_by_status: dict[str, int]
    daily: list[DailyWhatsApp]
    threads: list[WhatsAppThread]
    campaigns: list[WhatsAppCampaignStat]


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


@router.get("/whatsapp/overview", response_model=WhatsAppOverview)
async def read_whatsapp_overview(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    raw = await whatsapp_overview(db, ctx.tenant_id)
    raw["daily"] = [{"day": item["day"].isoformat(), "sent": item["sent"], "replies": item["replies"]} for item in raw["daily"]]
    raw["threads"] = [{**item, "conversation_id": str(item["conversation_id"]), "at": _iso(item["at"])} for item in raw["threads"]]
    raw["campaigns"] = [
        {
            **item,
            "id": str(item["id"]),
            "scheduled_at": item["scheduled_at"].isoformat() if item["scheduled_at"] else None,
            "created_at": _iso(item["created_at"]),
        }
        for item in raw["campaigns"]
    ]
    return raw
