"""Data-retention enforcement: delete transcripts (messages of voice
conversations) and clear recording links older than a tenant's policy."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.services.recordings import delete_provider_recording
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    Conversation,
    ConversationChannel,
    CrmLead,
    Message,
    WidgetLead,
)
from packages.database.models.platform import RetentionPolicy

logger = logging.getLogger(__name__)


def cutoff(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)


def expired_transcript_ids_query(tenant_id: uuid.UUID, days: int, now: datetime | None = None):
    """Messages of voice-call conversations whose call ended (or was created)
    before the cutoff. Keyed on the call, not the message, so a transcript is
    purged as a whole."""
    when = func.coalesce(Call.ended_at, Call.started_at, Call.created_at)
    return (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Call, Call.id == Conversation.call_id)
        .where(
            Message.tenant_id == tenant_id,
            Conversation.channel == ConversationChannel.VOICE,
            when < cutoff(days, now),
        )
    )


def expired_recordings_condition(tenant_id: uuid.UUID, days: int, now: datetime | None = None):
    when = func.coalesce(Call.ended_at, Call.started_at, Call.created_at)
    return (
        Call.tenant_id == tenant_id,
        Call.recording_url.is_not(None),
        when < cutoff(days, now),
    )


async def purge_old_leads(
    db: AsyncSession, tenant_id: uuid.UUID, days: int, now: datetime | None = None
) -> int:
    """Website-chat enquiries and CRM leads older than the policy that never became
    a customer. A lead that did become a customer is handled with the customer."""
    old = cutoff(days, now)
    widget = await db.execute(
        delete(WidgetLead).where(
            WidgetLead.tenant_id == tenant_id,
            WidgetLead.customer_id.is_(None),
            WidgetLead.created_at < old,
        )
    )
    crm = await db.execute(
        delete(CrmLead).where(
            CrmLead.tenant_id == tenant_id, CrmLead.customer_id.is_(None), CrmLead.created_at < old
        )
    )
    return (widget.rowcount or 0) + (crm.rowcount or 0)


async def purge_tenant(
    db: AsyncSession, policy: RetentionPolicy, now: datetime | None = None
) -> dict[str, int]:
    transcripts = recordings = recordings_failed = leads = 0
    if policy.transcript_days:
        result = await db.execute(
            delete(Message).where(
                Message.id.in_(
                    expired_transcript_ids_query(policy.tenant_id, policy.transcript_days, now)
                )
            )
        )
        transcripts = result.rowcount or 0
    if policy.recording_days:
        expired = (
            (
                await db.execute(
                    select(Call).where(
                        *expired_recordings_condition(policy.tenant_id, policy.recording_days, now)
                    )
                )
            )
            .scalars()
            .all()
        )
        for call in expired:
            if await delete_provider_recording(db, policy.tenant_id, call.recording_url):
                call.recording_url = None
                recordings += 1
            else:
                recordings_failed += 1
    if policy.lead_days:
        leads = await purge_old_leads(db, policy.tenant_id, policy.lead_days, now)
    policy.last_purged_at = now or datetime.now(UTC)
    summary = {
        "transcripts_deleted": transcripts,
        "recordings_cleared": recordings,
        "recordings_failed": recordings_failed,
        "leads_deleted": leads,
    }
    policy.last_purge_summary = summary
    await db.flush()
    return summary


async def purge_all_tenants(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    policies = (
        (await db.execute(select(RetentionPolicy).where(RetentionPolicy.enabled.is_(True))))
        .scalars()
        .all()
    )
    totals = {"tenants": 0, "transcripts_deleted": 0, "recordings_cleared": 0, "leads_deleted": 0}
    for policy in policies:
        try:
            result = await purge_tenant(db, policy, now)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("retention purge failed for tenant %s", policy.tenant_id)
            continue
        totals["tenants"] += 1
        totals["transcripts_deleted"] += result["transcripts_deleted"]
        totals["recordings_cleared"] += result["recordings_cleared"]
        totals["leads_deleted"] += result["leads_deleted"]
    return totals
