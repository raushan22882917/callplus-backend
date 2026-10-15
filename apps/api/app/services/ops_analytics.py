"""Agent, campaign and AI-handling analytics.

"Agent" means an AI agent: calls carry only ai_agent_id (no human-agent id is
stored), so human-agent breakdowns are not possible from current data.

Definitions
  resolved call   : analysed call whose outcome is in RESOLVED_OUTCOMES
  resolution rate : resolved / analysed calls (calls with an outcome)
  escalation rate : outcome == escalated / analysed calls
  AHT             : average duration_seconds of completed calls
  AI cost         : duration minutes * settings.ai_call_cost_paise_per_minute
Campaign funnel (calls are matched to a contact as the first outbound call by the
campaign's AI agent to that customer at/after the contact's attempt):
  calls      : contacts with a matching call
  connected  : the call completed and the caller spoke (outcome != no_conversation)
  qualified  : outcome in QUALIFIED_OUTCOMES
  converted  : outcome booking_requested, or a captured payment after the attempt
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    CampaignContact,
    Payment,
    PaymentLink,
    PaymentStatus,
)
from packages.database.models import User
from packages.database.models.insights import CallQAScore
from packages.database.models.workspace import CallHandoff

RESOLVED_OUTCOMES = {"resolved", "information_given", "booking_requested", "sale_interest"}
QUALIFIED_OUTCOMES = {"sale_interest", "booking_requested", "callback_requested"}


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def classify_contact(call: Call | None, *, paid_after_attempt: bool) -> dict:
    """Funnel flags for one campaign contact given its matched call (or None)."""
    if call is None:
        return {"called": False, "connected": False, "qualified": False, "converted": False}
    connected = call.status == CallStatus.COMPLETED and (
        call.outcome not in (None, "no_conversation")
        or (call.outcome is None and (call.duration_seconds or 0) > 0)
    )
    qualified = connected and call.outcome in QUALIFIED_OUTCOMES
    converted = connected and (call.outcome == "booking_requested" or paid_after_attempt)
    return {"called": True, "connected": connected, "qualified": qualified, "converted": converted}


def ai_cost_paise(total_seconds: int, paise_per_minute: int) -> int:
    return round(total_seconds / 60 * paise_per_minute)


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def agent_analytics(db: AsyncSession, tenant_id: uuid.UUID, days: int) -> list[dict]:
    completed = Call.status == CallStatus.COMPLETED
    analysed = Call.outcome.is_not(None)
    rows = (
        await db.execute(
            select(
                AIAgent.id,
                AIAgent.name,
                func.count(Call.id).label("calls"),
                func.count(Call.id).filter(completed).label("completed"),
                func.avg(Call.duration_seconds).filter(completed).label("aht"),
                func.count(Call.id).filter(analysed).label("analysed"),
                func.count(Call.id).filter(Call.outcome.in_(RESOLVED_OUTCOMES)).label("resolved"),
                func.count(Call.id).filter(Call.outcome == "escalated").label("escalated"),
            )
            .outerjoin(
                Call,
                (Call.ai_agent_id == AIAgent.id) & (Call.created_at >= _since(days)),
            )
            .where(AIAgent.tenant_id == tenant_id)
            .group_by(AIAgent.id, AIAgent.name)
            .order_by(func.count(Call.id).desc())
        )
    ).all()
    qa = {
        aid: (avg, n)
        for aid, avg, n in (
            await db.execute(
                select(Call.ai_agent_id, func.avg(CallQAScore.overall), func.count(CallQAScore.id))
                .join(Call, Call.id == CallQAScore.call_id)
                .where(CallQAScore.tenant_id == tenant_id, Call.created_at >= _since(days))
                .group_by(Call.ai_agent_id)
            )
        ).all()
    }
    return [
        {
            "agent_id": str(r.id),
            "name": r.name,
            "calls": r.calls,
            "completed": r.completed,
            "aht_seconds": round(float(r.aht)) if r.aht is not None else None,
            "resolution_rate": rate(r.resolved, r.analysed),
            "escalation_rate": rate(r.escalated, r.analysed),
            "avg_qa_score": round(float(qa[r.id][0]), 1) if r.id in qa else None,
            "qa_scored_calls": qa[r.id][1] if r.id in qa else 0,
        }
        for r in rows
    ]


async def human_agent_analytics(db: AsyncSession, tenant_id: uuid.UUID, days: int) -> list[dict]:
    """Per staff member, for calls they took over from the AI (workspace handoffs).
    A call counts for the person recorded as its handler."""
    since = _since(days)
    completed = Call.status == CallStatus.COMPLETED
    rows = (
        await db.execute(
            select(
                User.id,
                User.full_name,
                func.count(Call.id).label("calls"),
                func.count(Call.id).filter(completed).label("completed"),
                func.avg(Call.duration_seconds).filter(completed).label("aht"),
                func.count(Call.id).filter(Call.outcome.is_not(None)).label("analysed"),
                func.count(Call.id).filter(Call.outcome.in_(RESOLVED_OUTCOMES)).label("resolved"),
                func.count(Call.id).filter(Call.sentiment == "positive").label("positive"),
                func.count(Call.id).filter(Call.sentiment == "negative").label("negative"),
                func.avg(CallQAScore.overall).label("qa"),
            )
            .select_from(CallHandoff)
            .join(User, User.id == CallHandoff.handler_user_id)
            .join(Call, Call.id == CallHandoff.call_id)
            .outerjoin(CallQAScore, CallQAScore.call_id == Call.id)
            .where(
                CallHandoff.tenant_id == tenant_id,
                CallHandoff.handler_user_id.is_not(None),
                func.coalesce(CallHandoff.taken_at, Call.created_at) >= since,
            )
            .group_by(User.id, User.full_name)
            .order_by(func.count(Call.id).desc())
        )
    ).all()
    return [
        {
            "user_id": str(r.id),
            "name": r.full_name,
            "calls": r.calls,
            "completed": r.completed,
            "aht_seconds": round(float(r.aht)) if r.aht is not None else None,
            "resolution_rate": rate(r.resolved, r.analysed),
            "positive_calls": r.positive,
            "negative_calls": r.negative,
            "avg_qa_score": round(float(r.qa), 1) if r.qa is not None else None,
        }
        for r in rows
    ]


async def ai_analytics(db: AsyncSession, tenant_id: uuid.UUID, days: int) -> dict:
    since = _since(days)
    base = (Call.tenant_id == tenant_id, Call.ai_agent_id.is_not(None), Call.created_at >= since)
    row = (
        await db.execute(
            select(
                func.count(Call.id),
                func.count(Call.id).filter(Call.status == CallStatus.COMPLETED),
                func.count(Call.id).filter(Call.status.in_([CallStatus.FAILED, CallStatus.NO_ANSWER])),
                func.count(Call.id).filter(Call.outcome.is_not(None)),
                func.count(Call.id).filter(Call.outcome.in_(RESOLVED_OUTCOMES)),
                func.count(Call.id).filter(Call.outcome == "escalated"),
                func.coalesce(func.sum(Call.duration_seconds), 0),
                func.count(Call.id).filter(Call.duration_seconds > 0),
            ).where(*base)
        )
    ).one()
    total, completed, failed, analysed, resolved, escalated, seconds, billable = row
    per_minute = get_settings().ai_call_cost_paise_per_minute
    cost = ai_cost_paise(int(seconds), per_minute)
    daily = (
        await db.execute(
            select(
                func.date(Call.created_at).label("day"),
                func.count(Call.id),
                func.count(Call.id).filter(Call.outcome.in_(RESOLVED_OUTCOMES)),
                func.count(Call.id).filter(Call.outcome == "escalated"),
            )
            .where(*base)
            .group_by("day")
            .order_by("day")
        )
    ).all()
    return {
        "days": days,
        "ai_calls": total,
        "completed": completed,
        "failed_calls": failed,
        "analysed_calls": analysed,
        "resolution_rate": rate(resolved, analysed),
        "escalation_rate": rate(escalated, analysed),
        "failure_rate": rate(failed, total),
        "total_minutes": round(int(seconds) / 60, 1),
        "cost_paise_per_minute": per_minute,
        "total_cost_paise": cost,
        "cost_per_call_paise": round(cost / billable) if billable else None,
        "daily": [
            {"day": d.isoformat(), "calls": n, "resolved": r, "escalated": e} for d, n, r, e in daily
        ],
    }


async def campaign_analytics(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict]:
    campaigns = (
        (await db.execute(select(Campaign).where(Campaign.tenant_id == tenant_id).order_by(Campaign.created_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    out = []
    for campaign in campaigns:
        contacts = (
            (await db.execute(select(CampaignContact).where(CampaignContact.tenant_id == tenant_id, CampaignContact.campaign_id == campaign.id)))
            .scalars()
            .all()
        )
        calls_by_customer: dict = {}
        paid: dict = {}
        if campaign.ai_agent_id and contacts:
            ids = [c.customer_id for c in contacts]
            for call in (
                await db.execute(
                    select(Call)
                    .where(
                        Call.tenant_id == tenant_id,
                        Call.ai_agent_id == campaign.ai_agent_id,
                        Call.direction == CallDirection.OUTBOUND,
                        Call.customer_id.in_(ids),
                    )
                    .order_by(Call.created_at.asc())
                )
            ).scalars():
                calls_by_customer.setdefault(call.customer_id, []).append(call)
            for cid, at in (
                await db.execute(
                    select(PaymentLink.customer_id, Payment.created_at)
                    .join(PaymentLink, PaymentLink.id == Payment.payment_link_id)
                    .where(
                        Payment.tenant_id == tenant_id,
                        Payment.status == PaymentStatus.CAPTURED,
                        PaymentLink.customer_id.in_(ids),
                    )
                )
            ).all():
                paid.setdefault(cid, []).append(at)
        totals = {"called": 0, "connected": 0, "qualified": 0, "converted": 0}
        for contact in contacts:
            if contact.attempted_at is None:
                continue
            cutoff = contact.attempted_at - timedelta(minutes=1)
            matched = next((c for c in calls_by_customer.get(contact.customer_id, []) if c.created_at >= cutoff), None)
            paid_after = any(t >= contact.attempted_at for t in paid.get(contact.customer_id, []))
            flags = classify_contact(matched, paid_after_attempt=paid_after)
            for key in totals:
                totals[key] += int(flags[key])
        out.append(
            {
                "campaign_id": str(campaign.id),
                "name": campaign.name,
                "channel": campaign.channel.value,
                "status": campaign.status.value,
                "contacts": len(contacts),
                "attempted": sum(1 for c in contacts if c.attempted_at),
                "calls": totals["called"],
                "connected": totals["connected"],
                "qualified": totals["qualified"],
                "converted": totals["converted"],
                "connect_rate": rate(totals["connected"], totals["called"]),
                "conversion_rate": rate(totals["converted"], totals["connected"]),
            }
        )
    return out
