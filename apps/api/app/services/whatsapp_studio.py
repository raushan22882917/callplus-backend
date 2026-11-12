"""Numbers for the WhatsApp sidebar: chats, broadcasts, leads, and appointments."""

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.campaign import Campaign, CampaignChannel, CampaignContact
from packages.database.models.conversation import Conversation, ConversationChannel, Message, MessageDirection
from packages.database.models.crm import CrmDeal, CrmLead, CrmStage
from packages.database.models.customer import Customer
from packages.database.models.operations import Appointment
from packages.database.models.whatsapp import WhatsAppAccount
from packages.database.models.workflow import Workflow

from app.services.email_studio import _as_date, _value, fill_days


def fill_series(sent: dict[date, int], replies: dict[date, int], today: date, days: int = 30) -> list[dict]:
    days_sent = fill_days(sent, today, days)
    return [{"day": row["day"], "sent": row["count"], "replies": replies.get(row["day"], 0)} for row in days_sent]


async def whatsapp_overview(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    whatsapp_campaigns = select(Campaign.id).where(
        Campaign.tenant_id == tenant_id,
        Campaign.channel == CampaignChannel.WHATSAPP,
    )
    conversations = (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.tenant_id == tenant_id, Conversation.channel == ConversationChannel.WHATSAPP)
        )
    ).scalar_one()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    message_rows = (
        await db.execute(
            select(func.date(Message.created_at), Message.direction, func.count())
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.tenant_id == tenant_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
                Message.created_at >= since,
            )
            .group_by(func.date(Message.created_at), Message.direction)
        )
    ).all()
    sent_by_day: dict[date, int] = {}
    replies_by_day: dict[date, int] = {}
    sent_total = 0
    reply_total = 0
    for day, direction, count in message_rows:
        key = _as_date(day)
        amount = int(count)
        if direction == MessageDirection.OUTBOUND:
            sent_by_day[key] = amount
            sent_total += amount
        elif direction == MessageDirection.INBOUND:
            replies_by_day[key] = amount
            reply_total += amount

    delivery_rows = (
        await db.execute(
            select(Message.delivery_status, func.count())
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.tenant_id == tenant_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .group_by(Message.delivery_status)
        )
    ).all()

    customers_with_phone = (
        await db.execute(
            select(func.count())
            .select_from(Customer)
            .where(
                Customer.tenant_id == tenant_id,
                Customer.phone.isnot(None),
                func.length(func.trim(Customer.phone)) > 0,
            )
        )
    ).scalar_one()
    lead_rows = (
        await db.execute(
            select(CrmLead.stage, func.count()).where(CrmLead.tenant_id == tenant_id).group_by(CrmLead.stage)
        )
    ).all()
    leads = {_value(stage): int(count) for stage, count in lead_rows}
    won = (
        await db.execute(
            select(func.coalesce(func.sum(CrmDeal.value), 0)).where(
                CrmDeal.tenant_id == tenant_id,
                CrmDeal.stage == CrmStage.WON,
                CrmDeal.currency == "INR",
            )
        )
    ).scalar_one()
    now = datetime.now(timezone.utc)
    appointments = (
        await db.execute(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.tenant_id == tenant_id,
                Appointment.start_at >= now,
                Appointment.status != "cancelled",
            )
        )
    ).scalar_one()
    automations = (
        await db.execute(
            select(func.count()).select_from(Workflow).where(Workflow.tenant_id == tenant_id, Workflow.is_active.is_(True))
        )
    ).scalar_one()
    numbers = (
        await db.execute(
            select(func.count()).select_from(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id)
        )
    ).scalar_one()

    contact_rows = (
        await db.execute(
            select(CampaignContact.status, func.count())
            .where(CampaignContact.tenant_id == tenant_id, CampaignContact.campaign_id.in_(whatsapp_campaigns))
            .group_by(CampaignContact.status)
        )
    ).all()
    campaigns = list(
        (
            await db.execute(
                select(Campaign)
                .where(Campaign.tenant_id == tenant_id, Campaign.channel == CampaignChannel.WHATSAPP)
                .order_by(Campaign.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
    )
    tallies: dict[uuid.UUID, dict[str, int]] = {}
    if campaigns:
        count_rows = (
            await db.execute(
                select(CampaignContact.campaign_id, CampaignContact.status, func.count())
                .where(
                    CampaignContact.tenant_id == tenant_id,
                    CampaignContact.campaign_id.in_([campaign.id for campaign in campaigns]),
                )
                .group_by(CampaignContact.campaign_id, CampaignContact.status)
            )
        ).all()
        for campaign_id, status, count in count_rows:
            tallies.setdefault(campaign_id, {})[_value(status)] = int(count)

    recent_rows = (
        await db.execute(
            select(Message, Conversation.id, Customer.name)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .outerjoin(Customer, Customer.id == Conversation.customer_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.tenant_id == tenant_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
            )
            .order_by(Message.created_at.desc())
            .limit(40)
        )
    ).all()
    threads = []
    seen: set[uuid.UUID] = set()
    for message, conversation_id, customer_name in recent_rows:
        if conversation_id in seen:
            continue
        seen.add(conversation_id)
        threads.append(
            {
                "conversation_id": conversation_id,
                "customer_name": customer_name,
                "text": message.text or "",
                "at": message.created_at,
                "direction": _value(message.direction),
            }
        )
        if len(threads) == 6:
            break

    return {
        "conversations": int(conversations),
        "messages_sent": sent_total,
        "replies": reply_total,
        "customers_with_phone": int(customers_with_phone),
        "new_leads": leads.get("new_lead", 0),
        "qualified_leads": leads.get("qualified", 0),
        "won_inr": int(won or 0),
        "appointments": int(appointments),
        "automations": int(automations),
        "numbers": int(numbers),
        "delivery": {(status or "unknown"): int(count) for status, count in delivery_rows},
        "contacts_by_status": {_value(status): int(count) for status, count in contact_rows},
        "daily": fill_series(sent_by_day, replies_by_day, now.date()),
        "threads": threads,
        "campaigns": [
            {
                "id": campaign.id,
                "name": campaign.name,
                "status": _value(campaign.status),
                "scheduled_at": campaign.scheduled_at,
                "created_at": campaign.created_at,
                "sent": tallies.get(campaign.id, {}).get("sent", 0) + tallies.get(campaign.id, {}).get("completed", 0),
                "failed": tallies.get(campaign.id, {}).get("failed", 0),
                "pending": tallies.get(campaign.id, {}).get("pending", 0),
            }
            for campaign in campaigns
        ],
    }
