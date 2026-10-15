"""Usage metering and the itemized estimate.

AI minutes, human minutes and WhatsApp messages are derived from the calls /
messages tables (the source of truth). SMS and storage have no other table, so
they are read from `usage_events`, which also accepts adjustments for any
metric. Rates and limits come from the tenant's active `pricing_plans` row;
without one, indicative defaults from settings apply."""

import re
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.schemas.platform import LimitCheck, UsageLine, UsageSummary
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
)
from packages.database.models.platform import PricingPlan, UsageEvent

BYTES_PER_GB = 1024**3

METRIC_META: dict[str, tuple[str, str]] = {
    "ai_minutes": ("AI call minutes", "min"),
    "human_minutes": ("Human call minutes", "min"),
    "whatsapp_messages": ("WhatsApp messages (outbound)", "msg"),
    "storage_bytes": ("Storage", "GB"),
    "sms": ("SMS", "sms"),
}


def parse_month(
    month: str | None, *, now: datetime | None = None
) -> tuple[str, datetime, datetime]:
    """Returns (YYYY-MM, start, end) in UTC; defaults to the current month."""
    now = now or datetime.now(UTC)
    if month is None:
        year, mon = now.year, now.month
    else:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("month must be formatted YYYY-MM")
        year, mon = int(month[:4]), int(month[5:])
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), 1 if mon == 12 else mon + 1, 1, tzinfo=UTC)
    return f"{year:04d}-{mon:02d}", start, end


async def aggregate_usage(
    db: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime
) -> dict[str, float]:
    """Quantities per metric in their natural unit (minutes, messages, bytes, sms)."""
    when = func.coalesce(Call.started_at, Call.created_at)
    seconds = func.coalesce(Call.duration_seconds, 0)

    async def call_seconds(ai: bool) -> float:
        cond = Call.ai_agent_id.is_not(None) if ai else Call.ai_agent_id.is_(None)
        total = (
            await db.execute(
                select(func.coalesce(func.sum(seconds), 0)).where(
                    Call.tenant_id == tenant_id, cond, when >= start, when < end
                )
            )
        ).scalar_one()
        return float(total)

    whatsapp = (
        await db.execute(
            select(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
                Message.direction == MessageDirection.OUTBOUND,
                Message.created_at >= start,
                Message.created_at < end,
            )
        )
    ).scalar_one()

    totals = {
        "ai_minutes": round(await call_seconds(True) / 60, 2),
        "human_minutes": round(await call_seconds(False) / 60, 2),
        "whatsapp_messages": float(whatsapp),
        "storage_bytes": 0.0,
        "sms": 0.0,
    }
    rows = (
        await db.execute(
            select(UsageEvent.metric, func.coalesce(func.sum(UsageEvent.quantity), 0))
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.occurred_at >= start,
                UsageEvent.occurred_at < end,
            )
            .group_by(UsageEvent.metric)
        )
    ).all()
    for metric, qty in rows:
        if metric in totals:
            totals[metric] = round(totals[metric] + float(qty), 4)
    return totals


async def get_active_plan(db: AsyncSession, tenant_id: uuid.UUID) -> PricingPlan | None:
    return (
        await db.execute(
            select(PricingPlan)
            .where(PricingPlan.tenant_id == tenant_id, PricingPlan.is_active.is_(True))
            .order_by(PricingPlan.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def default_rates() -> dict[str, float]:
    settings = get_settings()
    per_minute = settings.usage_paise_per_call_minute / 100
    return {
        "ai_minutes": per_minute,
        "human_minutes": per_minute,
        "whatsapp_messages": settings.usage_paise_per_whatsapp_message / 100,
        "storage_bytes": 0.0,  # per GB
        "sms": 0.0,
    }


def build_summary(month: str, totals: dict[str, float], plan: PricingPlan | None) -> UsageSummary:
    if plan is not None:
        # An explicit plan is authoritative: unspecified metrics are free.
        rates = {m: float(plan.rates.get(m, 0)) for m in METRIC_META}
    else:
        rates = default_rates()
    limits = {k: float(v) for k, v in (plan.limits if plan else {}).items()}
    lines: list[UsageLine] = []
    for metric, (label, unit) in METRIC_META.items():
        quantity = totals.get(metric, 0.0)
        if metric == "storage_bytes":
            quantity = round(quantity / BYTES_PER_GB, 4)
        rate = rates.get(metric, 0.0)
        limit = limits.get(metric)
        lines.append(
            UsageLine(
                metric=metric,
                label=label,
                unit=unit,
                quantity=quantity,
                rate=rate,
                amount=round(quantity * rate, 2),
                limit=limit,
                over_limit=limit is not None and quantity > limit,
            )
        )
    base_fee = float(plan.base_fee) if plan else 0.0
    return UsageSummary(
        month=month,
        currency=plan.currency if plan else "INR",
        plan_id=plan.id if plan else None,
        plan_name=plan.name if plan else None,
        base_fee=base_fee,
        lines=lines,
        estimated_total=round(base_fee + sum(line.amount for line in lines), 2),
        rates_source="pricing_plan" if plan else "defaults",
    )


async def usage_summary(
    db: AsyncSession, tenant_id: uuid.UUID, month: str | None = None
) -> UsageSummary:
    label, start, end = parse_month(month)
    totals = await aggregate_usage(db, tenant_id, start, end)
    return build_summary(label, totals, await get_active_plan(db, tenant_id))


async def check_plan_limit(
    db: AsyncSession, tenant_id: uuid.UUID, metric: str, additional: float = 0
) -> LimitCheck:
    """Whether the tenant can consume `additional` more of `metric` this
    month under its active plan (no plan or no limit for the metric = allowed)."""
    summary = await usage_summary(db, tenant_id)
    line = next((entry for entry in summary.lines if entry.metric == metric), None)
    if line is None:
        raise ValueError(f"unknown metric {metric!r}")
    used = line.quantity
    if line.limit is None:
        return LimitCheck(metric=metric, used=used, limit=None, remaining=None, exceeded=False)
    return LimitCheck(
        metric=metric,
        used=used,
        limit=line.limit,
        remaining=max(line.limit - used, 0),
        exceeded=used + additional > line.limit,
    )


async def activate_plan(db: AsyncSession, plan: PricingPlan) -> None:
    """Only one active plan per tenant."""
    others = (
        await db.execute(
            select(PricingPlan).where(
                PricingPlan.tenant_id == plan.tenant_id,
                PricingPlan.id != plan.id,
                PricingPlan.is_active.is_(True),
            )
        )
    ).scalars()
    for other in others:
        other.is_active = False
    plan.is_active = True


class PlanInUse(ConflictError):
    detail = "Cannot delete the active pricing plan; activate another one first"
