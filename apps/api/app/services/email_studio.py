"""Numbers for the email sidebar: campaigns, sends, and saved messages."""

import uuid
from datetime import date, datetime, timedelta, timezone

from app.services.email_insights import engagement_by_campaign
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.campaign import Campaign, CampaignChannel, CampaignContact, CampaignContactStatus
from packages.database.models.customer import Customer


def fill_days(counts: dict[date, int], today: date, days: int = 30) -> list[dict]:
    """One row per day, including days with no sends, oldest first."""
    return [
        {"day": today - timedelta(days=offset), "count": counts.get(today - timedelta(days=offset), 0)}
        for offset in range(days - 1, -1, -1)
    ]


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


async def email_overview(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    email_campaigns = select(Campaign.id).where(
        Campaign.tenant_id == tenant_id,
        Campaign.channel == CampaignChannel.EMAIL,
    )
    status_rows = (
        await db.execute(
            select(Campaign.status, func.count())
            .where(Campaign.tenant_id == tenant_id, Campaign.channel == CampaignChannel.EMAIL)
            .group_by(Campaign.status)
        )
    ).all()
    contact_rows = (
        await db.execute(
            select(CampaignContact.status, func.count())
            .where(CampaignContact.tenant_id == tenant_id, CampaignContact.campaign_id.in_(email_campaigns))
            .group_by(CampaignContact.status)
        )
    ).all()
    customers_total = (
        await db.execute(select(func.count()).select_from(Customer).where(Customer.tenant_id == tenant_id))
    ).scalar_one()
    customers_with_email = (
        await db.execute(
            select(func.count())
            .select_from(Customer)
            .where(
                Customer.tenant_id == tenant_id,
                Customer.email.isnot(None),
                func.length(func.trim(Customer.email)) > 0,
            )
        )
    ).scalar_one()

    since = datetime.now(timezone.utc) - timedelta(days=30)
    sent_statuses = (CampaignContactStatus.SENT, CampaignContactStatus.COMPLETED, CampaignContactStatus.FAILED)
    day_rows = (
        await db.execute(
            select(func.date(CampaignContact.attempted_at), func.count())
            .where(
                CampaignContact.tenant_id == tenant_id,
                CampaignContact.campaign_id.in_(email_campaigns),
                CampaignContact.attempted_at.isnot(None),
                CampaignContact.attempted_at >= since,
                CampaignContact.status.in_(sent_statuses),
            )
            .group_by(func.date(CampaignContact.attempted_at))
        )
    ).all()
    by_day = {_as_date(row[0]): int(row[1]) for row in day_rows}

    campaigns = list(
        (
            await db.execute(
                select(Campaign)
                .where(Campaign.tenant_id == tenant_id, Campaign.channel == CampaignChannel.EMAIL)
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

    engagement = await engagement_by_campaign(db, tenant_id, [campaign.id for campaign in campaigns])
    total_sent = sum(
        tallies.get(c.id, {}).get("sent", 0) + tallies.get(c.id, {}).get("completed", 0) for c in campaigns
    )
    return {
        "opens": sum(e["opened"] for e in engagement.values()),
        "clicks": sum(e["clicked"] for e in engagement.values()),
        "delivered_basis": total_sent,
        "campaigns_by_status": {_value(status): int(count) for status, count in status_rows},
        "contacts_by_status": {_value(status): int(count) for status, count in contact_rows},
        "customers_with_email": int(customers_with_email),
        "customers_total": int(customers_total),
        "daily_sends": fill_days(by_day, datetime.now(timezone.utc).date()),
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
                "opened": engagement.get(campaign.id, {}).get("opened", 0),
                "clicked": engagement.get(campaign.id, {}).get("clicked", 0),
            }
            for campaign in campaigns
        ],
    }
