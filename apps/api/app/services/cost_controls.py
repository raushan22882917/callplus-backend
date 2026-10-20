"""Spend budget, alerts and safety caps.

Spend is the usage estimate from services/platform_usage (minutes and messages
times the tenant's rates), so the budget, the Billing page and the invoice
estimate can never disagree. The guard runs before every outbound call is
placed; inbound calls are never blocked.
"""

import calendar
import logging
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.integrations.email_client import send_email
from app.services.platform_usage import (
    aggregate_usage,
    build_summary,
    get_active_plan,
    parse_month,
)
from app.services.webhooks import enqueue_webhook_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, Call, CallDirection, CallStatus, Tenant
from packages.database.models.cost_controls import CostAlert, CostControls

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = [50, 80, 100]
# A call still "in flight" after this long is a stuck row, not a live call.
IN_FLIGHT_MAX_AGE = timedelta(minutes=45)
_BLOCK_LOG_COOLDOWN = timedelta(hours=1)
PAUSED_NOTE = "<p>Outbound calls are now paused until the budget is raised or the month resets.</p>"


class SpendGuardError(Exception):
    """An outbound call was refused by a budget or safety cap."""

    def __init__(self, code: str, message: str, http_status: int = 429) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


async def get_controls(db: AsyncSession, tenant_id: uuid.UUID) -> CostControls:
    """The tenant's row, created with defaults the first time it is read."""
    row = (
        await db.execute(select(CostControls).where(CostControls.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = CostControls(
            tenant_id=tenant_id,
            alert_thresholds=list(DEFAULT_THRESHOLDS),
            alert_emails=[],
            alerted_level=0,
            pause_outbound_at_budget=False,
        )
        db.add(row)
        await db.flush()
    return row


async def _controls_or_none(db: AsyncSession, tenant_id: uuid.UUID) -> CostControls | None:
    return (
        await db.execute(select(CostControls).where(CostControls.tenant_id == tenant_id))
    ).scalar_one_or_none()


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def crossed_level(percent: float, thresholds: list[int]) -> int:
    """Highest configured threshold at or below `percent` (0 if none)."""
    hit = [t for t in thresholds if percent >= t]
    return max(hit) if hit else 0


def forecast_month_end(spend: float, *, now: datetime) -> float:
    """Straight-line projection of this month's spend from the days elapsed."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed = (now.day - 1) + (now.hour + now.minute / 60) / 24
    if elapsed < 0.5:  # too early in the month to extrapolate honestly
        return round(spend, 2)
    return round(spend / elapsed * days_in_month, 2)


async def month_spend(
    db: AsyncSession, tenant_id: uuid.UUID, month: str | None = None
) -> tuple[str, float, str]:
    """(YYYY-MM, usage spend, currency). The plan's base fee is a fixed
    subscription, so it is not counted against a usage budget."""
    label, start, end = parse_month(month)
    totals = await aggregate_usage(db, tenant_id, start, end)
    summary = build_summary(label, totals, await get_active_plan(db, tenant_id))
    return label, round(sum(line.amount for line in summary.lines), 2), summary.currency


async def _outbound_today(db: AsyncSession, tenant_id: uuid.UUID, tz_name: str | None) -> int:
    tz = _tz(tz_name)
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        await db.execute(
            select(func.count(Call.id)).where(
                Call.tenant_id == tenant_id,
                Call.direction == CallDirection.OUTBOUND,
                Call.created_at >= local_midnight.astimezone(UTC),
            )
        )
    ).scalar_one()


async def _outbound_in_flight(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count(Call.id)).where(
                Call.tenant_id == tenant_id,
                Call.direction == CallDirection.OUTBOUND,
                Call.status.in_([CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]),
                Call.created_at >= datetime.now(UTC) - IN_FLIGHT_MAX_AGE,
            )
        )
    ).scalar_one()


async def status_snapshot(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    controls = await get_controls(db, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    period, spend, currency = await month_spend(db, tenant_id)
    budget = float(controls.monthly_budget) if controls.monthly_budget is not None else None
    percent = round(spend / budget * 100, 1) if budget else None
    thresholds = sorted(controls.alert_thresholds or DEFAULT_THRESHOLDS)
    today = await _outbound_today(db, tenant_id, tenant.timezone if tenant else None)
    in_flight = await _outbound_in_flight(db, tenant_id)
    paused = bool(controls.pause_outbound_at_budget and budget is not None and spend >= budget)
    return {
        "period": period,
        "currency": currency,
        "spend": spend,
        "budget": budget,
        "percent_used": percent,
        "level": crossed_level(percent, thresholds) if percent is not None else 0,
        "forecast": forecast_month_end(spend, now=datetime.now(UTC)),
        "outbound_paused": paused,
        "outbound_today": today,
        "daily_outbound_call_cap": controls.daily_outbound_call_cap,
        "outbound_in_flight": in_flight,
        "max_concurrent_outbound": controls.max_concurrent_outbound,
    }


async def _record_block(
    db: AsyncSession, tenant_id: uuid.UUID, *, message: str, spend: float, budget
) -> None:
    """Keep a visible trail of refusals without flooding it: one row per reason
    per hour."""
    recent = (
        await db.execute(
            select(CostAlert.id).where(
                CostAlert.tenant_id == tenant_id,
                CostAlert.kind == "blocked",
                CostAlert.message == message,
                CostAlert.created_at >= datetime.now(UTC) - _BLOCK_LOG_COOLDOWN,
            )
        )
    ).first()
    if recent is not None:
        return
    db.add(
        CostAlert(
            tenant_id=tenant_id,
            kind="blocked",
            period=datetime.now(UTC).strftime("%Y-%m"),
            level=0,
            spend=spend,
            budget=budget,
            message=message,
        )
    )
    await db.commit()


async def guard_outbound(db: AsyncSession, tenant_id: uuid.UUID, *, calls: int = 1) -> None:
    """Raises SpendGuardError if placing `calls` more outbound calls would break
    the tenant's budget or caps. Free (one cheap query) for a tenant that never
    configured controls."""
    controls = await _controls_or_none(db, tenant_id)
    if controls is None:
        return
    budget = float(controls.monthly_budget) if controls.monthly_budget is not None else None

    if controls.pause_outbound_at_budget and budget is not None:
        _, spend, currency = await month_spend(db, tenant_id)
        if spend >= budget:
            message = (
                f"Monthly budget of {budget:g} {currency} is used up "
                f"({spend:g} spent), so outbound calls are paused."
            )
            await _record_block(db, tenant_id, message=message, spend=spend, budget=budget)
            raise SpendGuardError("budget_exhausted", message, http_status=402)

    if controls.daily_outbound_call_cap is not None:
        tenant = await db.get(Tenant, tenant_id)
        today = await _outbound_today(db, tenant_id, tenant.timezone if tenant else None)
        if today + calls > controls.daily_outbound_call_cap:
            message = (
                f"Daily limit of {controls.daily_outbound_call_cap} outbound calls reached "
                f"({today} placed today)."
            )
            await _record_block(db, tenant_id, message=message, spend=0, budget=budget)
            raise SpendGuardError("daily_cap", message)

    if controls.max_concurrent_outbound is not None:
        live = await _outbound_in_flight(db, tenant_id)
        if live + calls > controls.max_concurrent_outbound:
            message = (
                f"{live} outbound calls are already in progress; the limit is "
                f"{controls.max_concurrent_outbound} at a time."
            )
            raise SpendGuardError("concurrency_cap", message)


async def evaluate_alerts(db: AsyncSession, tenant_id: uuid.UUID) -> CostAlert | None:
    """Raise one alert the first time spend crosses each threshold in a month.
    Best effort: callers wrap this so a failure never affects a call."""
    controls = await _controls_or_none(db, tenant_id)
    if controls is None or controls.monthly_budget is None or float(controls.monthly_budget) <= 0:
        return None
    budget = float(controls.monthly_budget)
    period, spend, currency = await month_spend(db, tenant_id)
    level = crossed_level(spend / budget * 100, sorted(controls.alert_thresholds or []))

    if controls.alerted_period != period:
        controls.alerted_period = period
        controls.alerted_level = 0
    if level <= controls.alerted_level:
        await db.commit()
        return None

    controls.alerted_level = level
    message = (
        f"Spend reached {level}% of the {budget:g} {currency} monthly budget "
        f"({spend:g} {currency} so far)."
    )
    alert = CostAlert(
        tenant_id=tenant_id,
        kind="budget",
        period=period,
        level=level,
        spend=spend,
        budget=budget,
        message=message,
    )
    db.add(alert)
    emails = list(controls.alert_emails or [])
    await db.commit()

    enqueue_webhook_event(
        tenant_id=tenant_id,
        event="budget.alert",
        data={
            "period": period,
            "level": level,
            "spend": spend,
            "budget": budget,
            "currency": currency,
            "outbound_paused": bool(controls.pause_outbound_at_budget and spend >= budget),
        },
    )
    for email in emails:
        try:
            await send_email(
                to_email=email,
                subject=f"Budget alert: {level}% of your monthly budget used",
                html_body=f"<p>{message}</p>"
                + (PAUSED_NOTE if controls.pause_outbound_at_budget and spend >= budget else ""),
            )
        except Exception:
            logger.warning("budget alert email to %s failed", email, exc_info=True)
    return alert


async def evaluate_alerts_safely(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    try:
        await evaluate_alerts(db, tenant_id)
    except Exception:
        logger.warning("budget evaluation failed for tenant %s", tenant_id, exc_info=True)
        await db.rollback()


def rates_for(summary_rates: dict[str, float]) -> tuple[float, float, float]:
    return (
        summary_rates.get("ai_minutes", 0.0),
        summary_rates.get("human_minutes", 0.0),
        summary_rates.get("whatsapp_messages", 0.0),
    )


async def cost_breakdown(db: AsyncSession, tenant_id: uuid.UUID, month: str | None = None) -> dict:
    """Where the month's money went: totals, a daily series, spend per AI agent
    and the costliest calls."""
    label, start, end = parse_month(month)
    plan = await get_active_plan(db, tenant_id)
    totals = await aggregate_usage(db, tenant_id, start, end)
    summary = build_summary(label, totals, plan)
    rates = {line.metric: line.rate for line in summary.lines}
    ai_rate, human_rate, _ = rates_for(rates)

    when = func.coalesce(Call.started_at, Call.created_at)
    day = func.date(when)
    seconds = func.coalesce(Call.duration_seconds, 0)
    is_ai = Call.ai_agent_id.is_not(None)

    daily_rows = (
        await db.execute(
            select(day, is_ai, func.sum(seconds), func.count(Call.id))
            .where(Call.tenant_id == tenant_id, when >= start, when < end)
            .group_by(day, is_ai)
            .order_by(day)
        )
    ).all()
    daily: dict[str, dict] = {}
    for d, ai, secs, count in daily_rows:
        entry = daily.setdefault(str(d), {"date": str(d), "cost": 0.0, "calls": 0, "minutes": 0.0})
        minutes = float(secs or 0) / 60
        entry["cost"] = round(entry["cost"] + minutes * (ai_rate if ai else human_rate), 2)
        entry["calls"] += int(count)
        entry["minutes"] = round(entry["minutes"] + minutes, 2)

    agent_rows = (
        await db.execute(
            select(Call.ai_agent_id, AIAgent.name, func.sum(seconds), func.count(Call.id))
            .join(AIAgent, AIAgent.id == Call.ai_agent_id)
            .where(Call.tenant_id == tenant_id, when >= start, when < end)
            .group_by(Call.ai_agent_id, AIAgent.name)
        )
    ).all()
    by_agent = sorted(
        (
            {
                "agent_id": str(agent_id),
                "name": name,
                "calls": int(count),
                "minutes": round(float(secs or 0) / 60, 2),
                "cost": round(float(secs or 0) / 60 * ai_rate, 2),
            }
            for agent_id, name, secs, count in agent_rows
        ),
        key=lambda row: -row["cost"],
    )

    top_rows = (
        await db.execute(
            select(Call.id, Call.to_number, Call.from_number, Call.direction, seconds, is_ai, when)
            .where(Call.tenant_id == tenant_id, when >= start, when < end)
            .order_by(seconds.desc())
            .limit(10)
        )
    ).all()
    top_calls = [
        {
            "call_id": str(call_id),
            "direction": direction.value,
            "number": to_number if direction == CallDirection.OUTBOUND else from_number,
            "minutes": round(float(secs) / 60, 2),
            "cost": round(float(secs) / 60 * (ai_rate if ai else human_rate), 2),
            "at": at.isoformat(),
        }
        for call_id, to_number, from_number, direction, secs, ai, at in top_rows
    ]

    call_minutes = totals["ai_minutes"] + totals["human_minutes"]
    call_count = sum(entry["calls"] for entry in daily.values())
    call_cost = round(totals["ai_minutes"] * ai_rate + totals["human_minutes"] * human_rate, 2)
    return {
        "month": label,
        "currency": summary.currency,
        "total": round(sum(line.amount for line in summary.lines), 2),
        "lines": [line.model_dump() for line in summary.lines],
        "daily": list(daily.values()),
        "by_agent": by_agent,
        "top_calls": top_calls,
        "avg_cost_per_call": round(call_cost / call_count, 2) if call_count else None,
        "avg_cost_per_minute": round(call_cost / call_minutes, 2) if call_minutes else None,
        "rates_source": summary.rates_source,
    }
