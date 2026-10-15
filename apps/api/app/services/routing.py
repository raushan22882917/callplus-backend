"""Intelligent transfer routing: which human agents should a transferred call
ring, and what to do when nobody can take it.

pick_targets() is the single entry point. It honours the tenant's business
hours / holidays (packages.database.models.workspace.BusinessHours), scores
AVAILABLE agents by language / skill / department / VIP fit, and returns
either an ordered list of phone numbers to ring or a voicemail / callback
fallback. When a tenant has configured no agent profiles at all it returns
action="legacy" so callers keep the original ring-every-TransferTarget behaviour.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, Call, CallStatus, Customer
from packages.database.models.workspace import (
    AgentProfile,
    BusinessHours,
    CallHandoff,
    HandlerState,
    Holiday,
    HumanAgentStatus,
)

MAX_TARGETS = 5

LANGUAGE_MATCH = 40
LANGUAGE_MISS_PENALTY = -20
DEPARTMENT_MATCH = 30
GENERALIST_BONUS = 5
SKILL_MATCH = 25
VIP_HANDLER_MATCH = 50
VIP_HANDLER_RESERVED_PENALTY = -5
LOAD_PENALTY = 10


@dataclass(frozen=True)
class RouteTarget:
    user_id: uuid.UUID
    phone_number: str
    score: int


@dataclass
class RoutingDecision:
    # "ring": ring `targets` | "voicemail"/"callback": nobody can take it
    # | "legacy": no agent profiles configured, caller uses its own targets.
    action: str
    reason: str
    targets: list[RouteTarget] = field(default_factory=list)
    message: str | None = None


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _lang_key(value: str | None) -> str:
    return _norm(value).replace("_", "-").split("-")[0]


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_open(
    *, timezone: str, windows: list[dict], holidays: set[date], now: datetime
) -> tuple[bool, str]:
    """Whether `now` falls inside a business window. Returns (open, reason)."""
    local = now.astimezone(_zone(timezone))
    if local.date() in holidays:
        return False, "holiday"
    for window in windows:
        if int(window["weekday"]) != local.weekday():
            continue
        if _parse_hhmm(window["start"]) <= local.time() < _parse_hhmm(window["end"]):
            return True, "within business hours"
    return False, "outside business hours"


def score_agent(
    agent: AgentProfile,
    *,
    language: str | None,
    department: str | None,
    vip: bool,
    issue_type: str | None,
    active_calls: int,
) -> int:
    score = 0
    agent_languages = {_lang_key(item) for item in agent.languages or []}
    if language:
        score += LANGUAGE_MATCH if _lang_key(language) in agent_languages else LANGUAGE_MISS_PENALTY
    departments = {_norm(item) for item in agent.departments or []}
    if department:
        if _norm(department) in departments:
            score += DEPARTMENT_MATCH
        elif not departments:
            score += GENERALIST_BONUS
    skills = {_norm(item) for item in agent.skills or []}
    if issue_type and _norm(issue_type) in skills:
        score += SKILL_MATCH
    if vip:
        score += VIP_HANDLER_MATCH if agent.is_vip_handler else 0
    elif agent.is_vip_handler:
        score += VIP_HANDLER_RESERVED_PENALTY
    score -= int(LOAD_PENALTY * active_calls / max(agent.max_concurrent, 1))
    return score


def rank_agents(
    agents: list[AgentProfile],
    active_calls: dict[uuid.UUID, int],
    *,
    language: str | None,
    department: str | None,
    vip: bool,
    issue_type: str | None,
) -> list[RouteTarget]:
    """Eligible = AVAILABLE, has a phone number and spare capacity. Highest score
    first; ties go to whoever has been available longest."""
    ranked: list[tuple[int, datetime, RouteTarget]] = []
    for agent in agents:
        if agent.status != HumanAgentStatus.AVAILABLE or not agent.phone_number:
            continue
        busy = active_calls.get(agent.user_id, 0)
        if busy >= max(agent.max_concurrent, 1):
            continue
        score = score_agent(
            agent,
            language=language,
            department=department,
            vip=vip,
            issue_type=issue_type,
            active_calls=busy,
        )
        since = agent.status_changed_at or agent.created_at or datetime.min.replace(tzinfo=UTC)
        ranked.append((score, since, RouteTarget(agent.user_id, agent.phone_number, score)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked][:MAX_TARGETS]


async def active_call_counts(db: AsyncSession, tenant_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(CallHandoff.handler_user_id, func.count())
        .join(Call, Call.id == CallHandoff.call_id)
        .where(
            CallHandoff.tenant_id == tenant_id,
            CallHandoff.state == HandlerState.HUMAN,
            CallHandoff.handler_user_id.is_not(None),
            Call.status.in_([CallStatus.IN_PROGRESS, CallStatus.RINGING]),
        )
        .group_by(CallHandoff.handler_user_id)
    )
    return {user_id: count for user_id, count in rows.all()}


async def pick_targets(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    language: str | None = None,
    department: str | None = None,
    vip: bool = False,
    issue_type: str | None = None,
    now: datetime | None = None,
) -> RoutingDecision:
    now = now or datetime.now(UTC)

    hours = (
        await db.execute(select(BusinessHours).where(BusinessHours.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if hours is not None and hours.windows:
        holidays = set(
            (await db.execute(select(Holiday.day).where(Holiday.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        open_now, why = is_open(
            timezone=hours.timezone, windows=hours.windows, holidays=holidays, now=now
        )
        if not open_now:
            return RoutingDecision(
                action=hours.fallback_action, reason=why, message=hours.fallback_message
            )

    agents = list(
        (await db.execute(select(AgentProfile).where(AgentProfile.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    if not any(agent.phone_number for agent in agents):
        return RoutingDecision(action="legacy", reason="no agent profiles configured")

    targets = rank_agents(
        agents,
        await active_call_counts(db, tenant_id),
        language=language,
        department=department,
        vip=vip,
        issue_type=issue_type,
    )
    if targets:
        return RoutingDecision(action="ring", reason="matched available agents", targets=targets)

    fallback = hours.fallback_action if hours is not None else "voicemail"
    return RoutingDecision(
        action=fallback,
        reason="no agent available",
        message=hours.fallback_message if hours is not None else None,
    )


async def route_call(db: AsyncSession, call: Call, reason: str | None = None) -> RoutingDecision:
    """pick_targets() with its inputs derived from the live call."""
    language = None
    if call.ai_agent_id:
        agent = await db.get(AIAgent, call.ai_agent_id)
        language = agent.language if agent else None
    vip = False
    if call.customer_id:
        customer = await db.get(Customer, call.customer_id)
        vip = bool(customer) and any(_norm(tag) == "vip" for tag in customer.tags or [])
    return await pick_targets(
        db,
        call.tenant_id,
        language=language,
        vip=vip,
        issue_type=call.intent or reason,
    )
