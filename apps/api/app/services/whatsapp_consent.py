"""Opt-in for WhatsApp marketing.

WhatsApp only lets a business send marketing messages to people who agreed to receive them, and a number that
reports too many unwanted messages loses its quality rating. So a campaign that sends a MARKETING template (or a
template this workspace has not registered here, which we cannot prove is anything else) goes only to numbers with
a granted "marketing" consent on record. Utility messages (appointment reminders, order updates) do not need it.
Consent is the same record the privacy pages and the email channel use; "STOP" withdraws it everywhere."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import privacy
from packages.database.models import ConsentRecord, WhatsAppTemplate


async def needs_consent(db: AsyncSession, tenant_id: uuid.UUID, template_name: str | None) -> bool:
    name = (template_name or "").strip()
    if not name:
        return True
    categories = (
        await db.execute(
            select(WhatsAppTemplate.category).where(WhatsAppTemplate.tenant_id == tenant_id, WhatsAppTemplate.name == name)
        )
    ).scalars().all()
    if not categories:
        return True  # not one of ours: treat it as marketing
    return any((c or "").upper() == "MARKETING" for c in categories)


async def consented_phones(db: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(ConsentRecord.subject_phone).where(
            ConsentRecord.tenant_id == tenant_id,
            ConsentRecord.purpose == "marketing",
            ConsentRecord.status == "granted",
            ConsentRecord.subject_phone.is_not(None),
        )
    )
    return set(rows.scalars().all())


async def has_marketing_consent(db: AsyncSession, tenant_id: uuid.UUID, phone: str | None) -> bool:
    clean = privacy._clean_phone(phone)
    if not clean:
        return False
    return (
        await db.execute(
            select(ConsentRecord.id).where(
                ConsentRecord.tenant_id == tenant_id,
                ConsentRecord.purpose == "marketing",
                ConsentRecord.status == "granted",
                ConsentRecord.subject_phone == clean,
            ).limit(1)
        )
    ).first() is not None


async def filter_audience(db: AsyncSession, tenant_id: uuid.UUID, customers: list) -> list:
    """Keeps the customers whose number has a granted marketing consent. One query, not one per customer."""
    have = await consented_phones(db, tenant_id)
    return [c for c in customers if privacy._clean_phone(c.phone) in have]
