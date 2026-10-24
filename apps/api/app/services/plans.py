"""Plan tiers: what each plan includes, and the checks that enforce it.

Why this is separate from `pricing_plans` (services/platform_usage.py): that
table is tenant-editable (a tenant can create and activate its own rates and
limits for its own estimates), so it cannot be trusted to enforce anything. The
entitlements here are owned by the platform: `tenants.plan_tier` is set at
sign-up and by the Razorpay webhook, and `tenants.plan_overrides` only by a
platform admin.

The numbers come from docs/BILLING.md and scripts/cost_model.py. In short: an AI
minute costs roughly INR 2.9 inbound and INR 6.6 outbound to serve (carrier, STT,
TTS, LLM), so the paid tiers are priced for at least ~30% gross margin even when
the allowance is fully used, and the free tier is capped so one sign-up costs a
few hundred rupees at worst.

Allowances are *hard* caps. Overage billing (charging per extra minute) needs
usage invoicing, which does not exist yet, so a tenant that reaches its
allowance is stopped (calls) or refused (new records) with a 402 that says how to
get more, unless a platform admin has granted `allow_overage` or bonus minutes.
"""

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.services.cost_controls import SpendGuardError
from app.services.platform_usage import aggregate_usage, parse_month
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    ApiKey,
    Call,
    CallDirection,
    CallStatus,
    KnowledgeDocument,
    MembershipStatus,
    PhoneNumber,
    Tenant,
    TenantUser,
)

Period = Literal["lifetime", "month"]

# Tenants created before plans existed. Never limited.
LEGACY = "legacy"


@dataclass(frozen=True)
class Tier:
    code: str
    name: str
    price_month_inr: int  # ex-GST; 0 = free, None-like custom handled by `custom`
    price_year_inr: int  # per year (ten months' price); 0 when not sold yearly
    overage_per_min_inr: float | None  # reference for future usage invoicing

    ai_minutes: int | None  # billable minutes, rounded up per call; None = unlimited
    ai_minutes_period: Period
    whatsapp_messages: int | None
    whatsapp_period: Period
    max_call_seconds: int | None  # hard cap on a single call
    max_concurrent_calls: int | None
    daily_outbound_calls: int | None

    phone_numbers: int | None
    agents: int | None
    team_members: int | None
    knowledge_documents: int | None
    api_keys: int | None

    campaigns: bool
    recording: bool
    custom: bool = False  # priced by conversation, not on the page
    blurb: str = ""

    @property
    def is_limited(self) -> bool:
        return self.code != LEGACY


FREE = Tier(
    code="free",
    name="Free",
    price_month_inr=0,
    price_year_inr=0,
    overage_per_min_inr=None,
    ai_minutes=10,
    ai_minutes_period="lifetime",
    whatsapp_messages=50,
    whatsapp_period="lifetime",
    max_call_seconds=300,
    max_concurrent_calls=1,
    daily_outbound_calls=5,
    phone_numbers=1,
    agents=1,
    team_members=2,
    knowledge_documents=5,
    api_keys=1,
    campaigns=False,
    recording=False,
    blurb="Try it end to end: 10 AI call minutes in total, no card needed.",
)
STARTER = Tier(
    code="starter",
    name="Starter",
    price_month_inr=2999,
    price_year_inr=29990,
    overage_per_min_inr=10.0,
    ai_minutes=300,
    ai_minutes_period="month",
    whatsapp_messages=1000,
    whatsapp_period="month",
    max_call_seconds=900,
    max_concurrent_calls=None,
    daily_outbound_calls=None,
    phone_numbers=2,
    agents=3,
    team_members=5,
    knowledge_documents=50,
    api_keys=5,
    campaigns=True,
    recording=True,
    blurb="For a small team putting one line on autopilot.",
)
GROWTH = Tier(
    code="growth",
    name="Growth",
    price_month_inr=12999,
    price_year_inr=129990,
    overage_per_min_inr=9.0,
    ai_minutes=1500,
    ai_minutes_period="month",
    whatsapp_messages=5000,
    whatsapp_period="month",
    max_call_seconds=1800,
    max_concurrent_calls=None,
    daily_outbound_calls=None,
    phone_numbers=5,
    agents=10,
    team_members=15,
    knowledge_documents=500,
    api_keys=20,
    campaigns=True,
    recording=True,
    blurb="For teams running real inbound and outbound volume.",
)
SCALE = Tier(
    code="scale",
    name="Scale",
    price_month_inr=54999,
    price_year_inr=549990,
    overage_per_min_inr=8.0,
    ai_minutes=6000,
    ai_minutes_period="month",
    whatsapp_messages=25000,
    whatsapp_period="month",
    max_call_seconds=3600,
    max_concurrent_calls=None,
    daily_outbound_calls=None,
    phone_numbers=20,
    agents=50,
    team_members=50,
    knowledge_documents=5000,
    api_keys=100,
    campaigns=True,
    recording=True,
    blurb="For large operations that need headroom.",
)
ENTERPRISE = Tier(
    code="enterprise",
    name="Enterprise",
    price_month_inr=0,
    price_year_inr=0,
    overage_per_min_inr=7.0,
    ai_minutes=None,
    ai_minutes_period="month",
    whatsapp_messages=None,
    whatsapp_period="month",
    max_call_seconds=None,
    max_concurrent_calls=None,
    daily_outbound_calls=None,
    phone_numbers=None,
    agents=None,
    team_members=None,
    knowledge_documents=None,
    api_keys=None,
    campaigns=True,
    recording=True,
    custom=True,
    blurb="Volume rates, custom limits and a close partner.",
)
PAYG = Tier(
    code="payg",
    name="Pay as you go",
    price_month_inr=0,
    price_year_inr=0,
    overage_per_min_inr=None,
    ai_minutes=None,  # not a plan allowance: calls are paid from the wallet (services/wallet.py)
    ai_minutes_period="month",
    whatsapp_messages=None,
    whatsapp_period="month",
    max_call_seconds=1800,
    max_concurrent_calls=5,
    daily_outbound_calls=None,
    phone_numbers=3,
    agents=5,
    team_members=10,
    knowledge_documents=100,
    api_keys=10,
    campaigns=True,
    recording=True,
    blurb="Top up a wallet and pay only for answered minutes and messages.",
)
_LEGACY_TIER = dataclasses.replace(
    ENTERPRISE, code=LEGACY, name="Legacy", custom=False, blurb="Pre-plan account."
)

TIERS: dict[str, Tier] = {
    t.code: t for t in (FREE, PAYG, STARTER, GROWTH, SCALE, ENTERPRISE, _LEGACY_TIER)
}
PUBLIC_TIERS = [FREE, STARTER, GROWTH, SCALE, ENTERPRISE]

# Override keys a platform admin may set. Anything else is ignored.
_INT_OVERRIDES = {
    "ai_minutes",
    "whatsapp_messages",
    "max_call_seconds",
    "max_concurrent_calls",
    "daily_outbound_calls",
    "phone_numbers",
    "agents",
    "team_members",
    "knowledge_documents",
    "api_keys",
}
_BOOL_OVERRIDES = {"campaigns", "recording"}


class PlanLimitError(SpendGuardError):
    """A plan allowance was reached. Subclasses SpendGuardError so every outbound
    call path, which already handles that, handles this without changes."""

    def __init__(self, code: str, message: str, *, tier: str = "", http_status: int = 402) -> None:
        super().__init__(code, message, http_status)
        self.tier = tier


def tier_for_plan_code(code: str) -> str | None:
    """Maps a billing `plans.code` ('growth', 'growth_annual') to a tier code."""
    base = code.removesuffix("_annual")
    return base if base in TIERS and base not in (LEGACY, "free") else None


def effective(tenant: Tenant) -> Tier:
    """The tenant's tier with any platform-admin overrides applied."""
    tier = TIERS.get(tenant.plan_tier or LEGACY, _LEGACY_TIER)
    overrides = tenant.plan_overrides or {}
    changes: dict = {}
    for key in _INT_OVERRIDES:
        if (
            key in overrides
            and isinstance(overrides[key], int)
            and not isinstance(overrides[key], bool)
        ):
            changes[key] = overrides[key]
    for key in _BOOL_OVERRIDES:
        if key in overrides and isinstance(overrides[key], bool):
            changes[key] = overrides[key]
    bonus = overrides.get("bonus_minutes")
    if isinstance(bonus, int) and not isinstance(bonus, bool) and bonus > 0:
        base = changes.get("ai_minutes", tier.ai_minutes)
        if base is not None:
            changes["ai_minutes"] = base + bonus
    return dataclasses.replace(tier, **changes) if changes else tier


def sanitize_overrides(raw: dict | None) -> dict:
    """Validates a platform admin's override map; unknown keys are rejected so a typo
    can't silently grant nothing."""
    allowed = _INT_OVERRIDES | _BOOL_OVERRIDES | {"bonus_minutes", "allow_overage"}
    clean: dict = {}
    for key, value in (raw or {}).items():
        if key not in allowed:
            raise ValueError(f"unknown override '{key}'")
        if key in _BOOL_OVERRIDES or key == "allow_overage":
            if not isinstance(value, bool):
                raise ValueError(f"'{key}' must be true or false")
        elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000:
            raise ValueError(f"'{key}' must be a whole number between 0 and 10,000,000")
        clean[key] = value
    return clean


def allows_overage(tenant: Tenant) -> bool:
    return bool((tenant.plan_overrides or {}).get("allow_overage"))


def _window(period: Period) -> tuple[datetime, datetime]:
    if period == "lifetime":
        return datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    _, start, end = parse_month(None)
    return start, end


# --- usage meters --------------------------------------------------------------


async def billable_minutes_used(db: AsyncSession, tenant_id: uuid.UUID, period: Period) -> int:
    """AI call minutes consumed, rounded UP per call: that is how the carrier
    bills (a 5 second call costs a full minute), and it stops many tiny calls
    from stretching a free allowance."""
    start, end = _window(period)
    when = func.coalesce(Call.started_at, Call.created_at)
    total = (
        await db.execute(
            select(func.coalesce(func.sum(func.ceil(Call.duration_seconds / 60.0)), 0)).where(
                Call.tenant_id == tenant_id,
                Call.ai_agent_id.is_not(None),
                Call.duration_seconds > 0,
                when >= start,
                when < end,
            )
        )
    ).scalar_one()
    return int(total)


async def whatsapp_used(db: AsyncSession, tenant_id: uuid.UUID, period: Period) -> int:
    start, end = _window(period)
    return int((await aggregate_usage(db, tenant_id, start, end))["whatsapp_messages"])


async def _count(db: AsyncSession, model, tenant_id: uuid.UUID, *extra) -> int:
    return int(
        (
            await db.execute(
                select(func.count(model.id)).where(model.tenant_id == tenant_id, *extra)
            )
        ).scalar_one()
    )


# resource -> (label, model, extra filters). Revoked keys and disabled members are
# not counted: they no longer cost the tenant anything.
_RESOURCES = {
    "agents": ("AI agents", AIAgent, ()),
    "phone_numbers": ("phone numbers", PhoneNumber, ()),
    "team_members": ("team members", TenantUser, (TenantUser.status != MembershipStatus.DISABLED,)),
    "knowledge_documents": ("knowledge documents", KnowledgeDocument, ()),
    "api_keys": ("API keys", ApiKey, (ApiKey.revoked_at.is_(None),)),
}

UPGRADE_HINT = "Upgrade your plan under Billing to raise this limit."

# A call still "in flight" after this long is a stuck row, not a live call.
_IN_FLIGHT = timedelta(minutes=45)


async def _tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    return await db.get(Tenant, tenant_id)


# --- checks (each raises PlanLimitError) ---------------------------------------


async def minutes_remaining(db: AsyncSession, tenant: Tenant) -> int | None:
    """Billable minutes left, or None when unlimited."""
    tier = effective(tenant)
    if tier.code == PAYG.code:
        from app.services import wallet

        return await wallet.minutes_available(db, tenant.id)
    if tier.ai_minutes is None or allows_overage(tenant):
        return None
    used = await billable_minutes_used(db, tenant.id, tier.ai_minutes_period)
    return max(tier.ai_minutes - used, 0)


async def ensure_minutes_available(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Refuses a new AI call once the minutes allowance is spent."""
    tenant = await _tenant(db, tenant_id)
    if tenant is None:
        return
    if effective(tenant).code == PAYG.code:
        from app.services import wallet

        await wallet.ensure_call_balance(db, tenant_id)
        return
    remaining = await minutes_remaining(db, tenant)
    if remaining is not None and remaining <= 0:
        tier = effective(tenant)
        scope = "free minutes are" if tier.code == "free" else "monthly minutes are"
        raise PlanLimitError(
            "plan_minutes_exhausted",
            f"Your {tier.name} plan's {tier.ai_minutes} AI call {scope} used up. {UPGRADE_HINT}",
            tier=tier.code,
        )


async def effective_max_call_seconds(
    db: AsyncSession, tenant_id: uuid.UUID, agent_cap_seconds: int
) -> int:
    """The longest this call may run: the agent's own cap, the plan's per-call cap,
    and the minutes left in the allowance, whichever is smallest. This is what
    makes a 10-minute free allowance end the call at 10 minutes instead of
    overshooting it."""
    tenant = await _tenant(db, tenant_id)
    if tenant is None:
        return agent_cap_seconds
    tier = effective(tenant)
    cap = agent_cap_seconds
    if tier.max_call_seconds is not None:
        cap = min(cap, tier.max_call_seconds)
    remaining = await minutes_remaining(db, tenant)
    if remaining is not None:
        cap = min(cap, max(remaining, 1) * 60)
    return max(cap, 1)


async def ensure_outbound_allowed(db: AsyncSession, tenant_id: uuid.UUID, calls: int = 1) -> None:
    """Plan checks for placing an outbound call (hooked into guard_outbound)."""
    tenant = await _tenant(db, tenant_id)
    if tenant is None or not effective(tenant).is_limited:
        return
    tier = effective(tenant)
    await ensure_minutes_available(db, tenant_id)

    if tier.max_concurrent_calls is not None:
        live = await _count(
            db,
            Call,
            tenant_id,
            Call.ai_agent_id.is_not(None),
            Call.status.in_([CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]),
            Call.created_at >= datetime.now(UTC).replace(microsecond=0) - _IN_FLIGHT,
        )
        if live + calls > tier.max_concurrent_calls:
            raise PlanLimitError(
                "plan_concurrency",
                f"Your {tier.name} plan allows {tier.max_concurrent_calls} call at a time. "
                f"Wait for the current call to end. {UPGRADE_HINT}",
                tier=tier.code,
                http_status=429,
            )

    if tier.daily_outbound_calls is not None:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        today = await _count(
            db, Call, tenant_id, Call.direction == CallDirection.OUTBOUND, Call.created_at >= start
        )
        if today + calls > tier.daily_outbound_calls:
            raise PlanLimitError(
                "plan_daily_outbound",
                f"Your {tier.name} plan allows {tier.daily_outbound_calls} outbound calls a day "
                f"({today} placed today). {UPGRADE_HINT}",
                tier=tier.code,
                http_status=429,
            )


async def ensure_inbound_allowed(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Plan checks before answering an inbound call with the AI."""
    tenant = await _tenant(db, tenant_id)
    if tenant is None or not effective(tenant).is_limited:
        return
    tier = effective(tenant)
    await ensure_minutes_available(db, tenant_id)
    if tier.max_concurrent_calls is not None:
        live = await _count(
            db,
            Call,
            tenant_id,
            Call.ai_agent_id.is_not(None),
            Call.status == CallStatus.IN_PROGRESS,
            Call.created_at >= datetime.now(UTC) - _IN_FLIGHT,
        )
        if live >= tier.max_concurrent_calls:
            raise PlanLimitError(
                "plan_concurrency",
                f"Your {tier.name} plan allows {tier.max_concurrent_calls} call at a time.",
                tier=tier.code,
                http_status=429,
            )


async def ensure_whatsapp_available(db: AsyncSession, tenant_id: uuid.UUID, count: int = 1) -> None:
    tenant = await _tenant(db, tenant_id)
    if tenant is None:
        return
    tier = effective(tenant)
    if tier.code == PAYG.code:
        from app.services import wallet

        await wallet.ensure_balance(db, tenant_id, wallet.WHATSAPP_MESSAGE_PAISE * count, "this message")
        return
    if tier.whatsapp_messages is None or allows_overage(tenant):
        return
    used = await whatsapp_used(db, tenant_id, tier.whatsapp_period)
    if used + count > tier.whatsapp_messages:
        raise PlanLimitError(
            "plan_whatsapp_exhausted",
            f"Your {tier.name} plan's {tier.whatsapp_messages} WhatsApp messages are used up. "
            f"{UPGRADE_HINT}",
            tier=tier.code,
        )


async def ensure_can_create(db: AsyncSession, tenant_id: uuid.UUID, resource: str) -> None:
    """Refuses creating one more `resource` when the plan's count is reached."""
    tenant = await _tenant(db, tenant_id)
    if tenant is None:
        return
    tier = effective(tenant)
    limit = getattr(tier, resource)
    if limit is None:
        return
    label, model, extra = _RESOURCES[resource]
    current = await _count(db, model, tenant_id, *extra)
    if current >= limit:
        raise PlanLimitError(
            f"plan_{resource}_limit",
            f"Your {tier.name} plan includes {limit} {label} and you have {current}. "
            f"{UPGRADE_HINT}",
            tier=tier.code,
        )


async def remaining(db: AsyncSession, tenant_id: uuid.UUID, resource: str) -> int | None:
    """How many more `resource` rows the plan allows (None for unlimited)."""
    tenant = await _tenant(db, tenant_id)
    if tenant is None:
        return None
    limit = getattr(effective(tenant), resource)
    if limit is None:
        return None
    _label, model, extra = _RESOURCES[resource]
    return max(0, limit - await _count(db, model, tenant_id, *extra))


async def ensure_feature(db: AsyncSession, tenant_id: uuid.UUID, feature: str) -> None:
    tenant = await _tenant(db, tenant_id)
    if tenant is None:
        return
    tier = effective(tenant)
    if getattr(tier, feature):
        return
    label = {"campaigns": "Outbound campaigns", "recording": "Call recording"}[feature]
    raise PlanLimitError(
        f"plan_{feature}_unavailable",
        f"{label} aren't included in the {tier.name} plan. {UPGRADE_HINT}",
        tier=tier.code,
    )


# --- snapshot for the Billing page --------------------------------------------


def _meter(key: str, label: str, used: int, limit: int | None, period: str, unit: str) -> dict:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "used": used,
        "limit": limit,
        "remaining": None if limit is None else max(limit - used, 0),
        "percent": None if not limit else round(min(used / limit, 1) * 100, 1),
        "period": period,
    }


async def entitlements(db: AsyncSession, tenant: Tenant) -> dict:
    tier = effective(tenant)
    minutes = await billable_minutes_used(db, tenant.id, tier.ai_minutes_period)
    wa = await whatsapp_used(db, tenant.id, tier.whatsapp_period)
    meters = [
        _meter(
            "ai_minutes", "AI call minutes", minutes, tier.ai_minutes, tier.ai_minutes_period, "min"
        ),
        _meter(
            "whatsapp_messages",
            "WhatsApp messages",
            wa,
            tier.whatsapp_messages,
            tier.whatsapp_period,
            "msg",
        ),
    ]
    for key, (label, model, extra) in _RESOURCES.items():
        count = await _count(db, model, tenant.id, *extra)
        meters.append(_meter(key, label.capitalize(), count, getattr(tier, key), "total", "count"))
    return {
        "tier": tier.code,
        "name": tier.name,
        "is_free": tier.code == "free",
        "max_call_seconds": tier.max_call_seconds,
        "max_concurrent_calls": tier.max_concurrent_calls,
        "daily_outbound_calls": tier.daily_outbound_calls,
        "campaigns": tier.campaigns,
        "recording": tier.recording,
        "allow_overage": allows_overage(tenant),
        "meters": meters,
        "price_month_inr": tier.price_month_inr,
    }


def public_catalogue() -> list[dict]:
    """The tiers as shown on the pricing page and the upgrade screen."""
    out = []
    for t in PUBLIC_TIERS:
        d = dataclasses.asdict(t)
        d["monthly_equivalent_if_annual_inr"] = (
            round(t.price_year_inr / 12) if t.price_year_inr else 0
        )
        out.append(d)
    return out


__all__ = [
    "PUBLIC_TIERS",
    "TIERS",
    "Tier",
    "PlanLimitError",
    "billable_minutes_used",
    "effective",
    "effective_max_call_seconds",
    "ensure_can_create",
    "ensure_feature",
    "ensure_inbound_allowed",
    "ensure_minutes_available",
    "ensure_outbound_allowed",
    "ensure_whatsapp_available",
    "entitlements",
    "minutes_remaining",
    "public_catalogue",
    "tier_for_plan_code",
]
