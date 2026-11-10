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

Allowances are hard caps. A tenant that reaches its allowance is stopped with a
402 that says how to get more, unless a platform admin has granted bonus minutes
or `allow_overage`. With `allow_overage`, the extra minutes and messages are
allowed and written onto a pending invoice (`sync_overage_invoice`).
"""

import dataclasses
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.services.cost_controls import SpendGuardError
from app.services.platform_usage import aggregate_usage, parse_month
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    ApiKey,
    Call,
    CallDirection,
    CallStatus,
    Invoice,
    InvoiceStatus,
    KnowledgeDocument,
    MembershipStatus,
    PhoneNumber,
    Role,
    Tenant,
    TenantUser,
    UsageEvent,
    User,
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
    overage_per_min_inr: float | None  # charged on the overage invoice when allow_overage is set

    ai_minutes: int | None  # billable minutes, rounded up per call; None = unlimited
    ai_minutes_period: Period
    whatsapp_messages: int | None
    whatsapp_period: Period
    sms_messages: int | None  # segments; None = not capped by the plan
    sms_period: Period
    email_messages: int | None  # customer emails; login and verification mail is not counted
    email_period: Period
    social_messages: int | None  # Instagram and Messenger replies
    social_period: Period
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
    sms_messages=20,
    sms_period="lifetime",
    email_messages=30,
    email_period="lifetime",
    social_messages=50,
    social_period="lifetime",
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
    sms_messages=300,
    sms_period="month",
    email_messages=500,
    email_period="month",
    social_messages=1000,
    social_period="month",
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
    sms_messages=1500,
    sms_period="month",
    email_messages=2000,
    email_period="month",
    social_messages=5000,
    social_period="month",
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
    sms_messages=8000,
    sms_period="month",
    email_messages=10000,
    email_period="month",
    social_messages=25000,
    social_period="month",
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
    sms_messages=None,
    sms_period="month",
    email_messages=None,
    email_period="month",
    social_messages=None,
    social_period="month",
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
    sms_messages=None,
    sms_period="month",
    email_messages=None,
    email_period="month",
    social_messages=None,
    social_period="month",
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
    # A call on a number we pay Twilio for costs us even when a person, not the
    # AI, talks. The AI's own calls count whether or not the number is ours.
    ours = select(PhoneNumber.e164_number).where(
        PhoneNumber.tenant_id == tenant_id, PhoneNumber.platform_owned.is_(True)
    )
    total = (
        await db.execute(
            select(
                func.coalesce(func.sum(func.ceil(Call.duration_seconds / 60.0) * Call.cost_multiplier), 0)
            ).where(
                Call.tenant_id == tenant_id,
                Call.duration_seconds > 0,
                when >= start,
                when < end,
                or_(
                    Call.ai_agent_id.is_not(None),
                    Call.to_number.in_(ours),
                    Call.from_number.in_(ours),
                ),
            )
        )
    ).scalar_one()
    # A call on a premium provider of the platform's own counts more than its minutes (Call.cost_multiplier).
    return math.ceil(float(total))


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
# Said to a caller when the business cannot take the call. The caller is the
# business's customer and should not hear about plans or verification.
CALLER_UNAVAILABLE = "Sorry, we can't take your call right now. Please try again later."
RING_LEG_METRIC = "ring_legs"

# A call still "in flight" after this long is a stuck row, not a live call.
_IN_FLIGHT = timedelta(minutes=45)


async def _tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    return await db.get(Tenant, tenant_id)


async def _lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    """Serializes spend checks for one workspace.

    Two requests that both read "0 minutes used" and then both place a call
    would each pass. Holding this row until the caller commits makes the second
    request see the first request's call or usage row.
    """
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    return result.scalar_one_or_none()


async def ensure_owner_verified(db: AsyncSession, tenant: Tenant) -> None:
    """A workspace that is not on the pre-plan legacy tier must have a verified
    owner before it can spend money. No owner row (a system tenant) is allowed
    through: there is nobody to verify. Legacy is skipped because those accounts
    predate verification and the test suite signs up onto it."""
    if (tenant.plan_tier or LEGACY) == LEGACY:
        return
    row = (
        await db.execute(
            select(User.email_verified_at)
            .join(TenantUser, TenantUser.user_id == User.id)
            .join(Role, Role.id == TenantUser.role_id)
            .where(
                TenantUser.tenant_id == tenant.id,
                TenantUser.status == MembershipStatus.ACTIVE,
                Role.name == "OWNER",
            )
            .limit(1)
        )
    ).first()
    if row is None or row[0] is not None:
        return
    raise PlanLimitError(
        "email_unverified",
        "Verify your email before this workspace can call, buy a number, or send messages.",
        tier=effective(tenant).code,
        http_status=403,
    )


async def _usage_quantity(
    db: AsyncSession, tenant_id: uuid.UUID, metric: str, start: datetime, end: datetime
) -> int:
    total = (
        await db.execute(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.metric == metric,
                UsageEvent.occurred_at >= start,
                UsageEvent.occurred_at < end,
            )
        )
    ).scalar_one()
    return int(float(total))


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


async def _included_ai(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """True when calls use CallPlus's Sarvam or model, so a plan or wallet minute applies."""
    from app.integrations.credentials import resolve_key, resolve_llm
    from app.integrations.providers import SARVAM

    _key, llm_source = await resolve_llm(db, tenant_id)
    if llm_source == "managed":
        return True
    _key, sarvam_source = await resolve_key(db, tenant_id, SARVAM)
    return sarvam_source == "managed"


async def ensure_minutes_available(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Refuses a new AI call once the minutes allowance is spent."""
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await ensure_owner_verified(db, tenant)
    if not await _included_ai(db, tenant_id):
        # Own Sarvam or language-model key: the workspace pays that provider.
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
    db: AsyncSession, tenant_id: uuid.UUID, agent_cap_seconds: int, *, cost_multiplier: float = 1.0
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
    if remaining is not None and cost_multiplier > 0:
        # Each minute of this call counts `cost_multiplier` minutes, so fewer real minutes are left.
        # A multiplier of 0 is the workspace's own key: the plan allowance does not end the call.
        cap = min(cap, max(int(max(remaining, 1) * 60 / cost_multiplier), 1))
    return max(cap, 1)


async def ensure_outbound_allowed(db: AsyncSession, tenant_id: uuid.UUID, calls: int = 1) -> None:
    """Plan checks for placing an outbound call (hooked into guard_outbound)."""
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await ensure_owner_verified(db, tenant)
    if not effective(tenant).is_limited:
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


async def ensure_inbound_allowed(
    db: AsyncSession, tenant_id: uuid.UUID, *, exclude_call_id: uuid.UUID | None = None
) -> None:
    """Plan checks before answering an inbound call with the AI.

    `exclude_call_id` is the call already created for this webhook, so a second
    check later in the same call (a menu digit that reaches the agent) does not
    count the call against itself.
    """
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await ensure_owner_verified(db, tenant)
    if not effective(tenant).is_limited:
        return
    tier = effective(tenant)
    await ensure_minutes_available(db, tenant_id)
    if tier.max_concurrent_calls is not None:
        live_filters = [
            Call.ai_agent_id.is_not(None),
            Call.status.in_([CallStatus.QUEUED, CallStatus.RINGING, CallStatus.IN_PROGRESS]),
            Call.created_at >= datetime.now(UTC) - _IN_FLIGHT,
        ]
        if exclude_call_id is not None:
            live_filters.append(Call.id != exclude_call_id)
        live = await _count(db, Call, tenant_id, *live_filters)
        if live >= tier.max_concurrent_calls:
            raise PlanLimitError(
                "plan_concurrency",
                f"Your {tier.name} plan allows {tier.max_concurrent_calls} call at a time.",
                tier=tier.code,
                http_status=429,
            )


async def ensure_whatsapp_available(db: AsyncSession, tenant_id: uuid.UUID, count: int = 1) -> None:
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await ensure_owner_verified(db, tenant)
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


async def _refuse_sms_if_needed(db: AsyncSession, tenant: Tenant, segments: int) -> None:
    await ensure_owner_verified(db, tenant)
    tier = effective(tenant)
    if tier.code == PAYG.code:
        from app.services import wallet

        await wallet.ensure_balance(db, tenant.id, wallet.SMS_PAISE * segments, "this SMS")
        return
    if tier.sms_messages is None or allows_overage(tenant):
        return
    start, end = _window(tier.sms_period)
    used = await _usage_quantity(db, tenant.id, "sms", start, end)
    if used + segments > tier.sms_messages:
        raise PlanLimitError(
            "plan_sms_exhausted",
            f"Your {tier.name} plan's {tier.sms_messages} SMS segments are used up. {UPGRADE_HINT}",
            tier=tier.code,
        )


async def ensure_sms_available(db: AsyncSession, tenant_id: uuid.UUID, segments: int = 1) -> None:
    """Refuses an SMS once the plan's segment allowance is spent. Does not record it."""
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await _refuse_sms_if_needed(db, tenant, segments)


async def reserve_sms(db: AsyncSession, tenant_id: uuid.UUID, segments: int = 1) -> UsageEvent:
    """Checks the SMS allowance and records the segments in this transaction.

    The row is the reservation: a second request blocked on the tenant lock
    sees it and cannot pass the same last segment. The caller deletes the row
    if the provider send then fails, before committing.
    """
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        raise PlanLimitError("plan_sms_exhausted", "Workspace not found.", tier="")
    await _refuse_sms_if_needed(db, tenant, segments)
    event = UsageEvent(
        tenant_id=tenant_id,
        metric="sms",
        quantity=segments,
        occurred_at=datetime.now(UTC),
        extra={"segments": segments},
    )
    db.add(event)
    await db.flush()
    return event


async def _refuse_email_if_needed(db: AsyncSession, tenant: Tenant) -> None:
    await ensure_owner_verified(db, tenant)
    tier = effective(tenant)
    if tier.code == PAYG.code:
        from app.services import wallet

        await wallet.ensure_balance(db, tenant.id, wallet.EMAIL_PAISE, "this email")
        return
    if tier.email_messages is None or allows_overage(tenant):
        return
    start, end = _window(tier.email_period)
    used = await _usage_quantity(db, tenant.id, "email", start, end)
    if used + 1 > tier.email_messages:
        raise PlanLimitError(
            "plan_email_exhausted",
            f"Your {tier.name} plan's {tier.email_messages} emails are used up. {UPGRADE_HINT}",
            tier=tier.code,
        )


async def ensure_email_available(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Refuses one customer email once the allowance is spent. Does not record it."""
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await _refuse_email_if_needed(db, tenant)


async def reserve_email(db: AsyncSession, tenant_id: uuid.UUID) -> UsageEvent:
    """Checks the email allowance and records one message in this transaction.

    Login, verification and password mail do not come through here. The caller
    deletes the row if the mail is not accepted, before committing.
    """
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        raise PlanLimitError("plan_email_exhausted", "Workspace not found.", tier="")
    await _refuse_email_if_needed(db, tenant)
    event = UsageEvent(
        tenant_id=tenant_id,
        metric="email",
        quantity=1,
        occurred_at=datetime.now(UTC),
        extra={"kind": "customer"},
    )
    db.add(event)
    await db.flush()
    return event


async def _refuse_social_if_needed(db: AsyncSession, tenant: Tenant) -> None:
    await ensure_owner_verified(db, tenant)
    tier = effective(tenant)
    if tier.code == PAYG.code:
        from app.services import wallet

        await wallet.ensure_balance(db, tenant.id, wallet.SOCIAL_PAISE, "this message")
        return
    if tier.social_messages is None or allows_overage(tenant):
        return
    start, end = _window(tier.social_period)
    used = await _usage_quantity(db, tenant.id, "social", start, end)
    if used + 1 > tier.social_messages:
        raise PlanLimitError(
            "plan_social_exhausted",
            f"Your {tier.name} plan's {tier.social_messages} Instagram and Messenger "
            f"messages are used up. {UPGRADE_HINT}",
            tier=tier.code,
        )


async def ensure_social_available(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await _refuse_social_if_needed(db, tenant)


async def reserve_social(db: AsyncSession, tenant_id: uuid.UUID) -> UsageEvent:
    """Records one Instagram or Messenger reply against the plan."""
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        raise PlanLimitError("plan_social_exhausted", "Workspace not found.", tier="")
    await _refuse_social_if_needed(db, tenant)
    event = UsageEvent(
        tenant_id=tenant_id,
        metric="social",
        quantity=1,
        occurred_at=datetime.now(UTC),
        extra={"kind": "social"},
    )
    db.add(event)
    await db.flush()
    return event


async def claim_whatsapp_send(tenant_id: uuid.UUID) -> uuid.UUID:
    """Reserves one WhatsApp send and commits it before the provider call.

    The row is visible to the next request immediately, so two sends cannot
    both pass the last message. The caller deletes it if the provider rejects
    the send. Paths that also save a Message pass metered=False and do not
    claim, because the message row is what gets counted.
    """
    from packages.database.session import session_scope

    async with session_scope() as db:
        await ensure_whatsapp_available(db, tenant_id)
        event = UsageEvent(
            tenant_id=tenant_id,
            metric="whatsapp_messages",
            quantity=1,
            occurred_at=datetime.now(UTC),
            extra={"source": "send"},
        )
        db.add(event)
        await db.flush()
        event_id = event.id
        await sync_overage_invoice(db, tenant_id)
        await db.commit()
        return event_id


async def release_whatsapp_claim(event_id: uuid.UUID) -> None:
    """Drops a WhatsApp reservation after the provider refused the send."""
    from packages.database.session import session_scope

    async with session_scope() as db:
        event = await db.get(UsageEvent, event_id)
        if event is not None and (event.extra or {}).get("source") == "send":
            tenant_id = event.tenant_id
            await db.delete(event)
            await db.flush()
            await sync_overage_invoice(db, tenant_id)
            await db.commit()


async def ensure_platform_number_purchase(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """The Free plan may register a number the tenant already pays for.
    It may not buy one on the platform's Twilio account."""
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return
    await ensure_owner_verified(db, tenant)
    tier = effective(tenant)
    if tier.code != "free":
        return
    raise PlanLimitError(
        "plan_platform_number",
        "The Free plan cannot buy a phone number on the platform account. "
        "Connect your own Twilio account, or upgrade. "
        + UPGRADE_HINT,
        tier=tier.code,
    )


async def claim_ring_leg(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Reserves one outbound carrier leg (queue or ring group).

    Returns False when today's allowance is used up. Raises when the owner
    still has to verify their email. Paid plans with no daily cap always
    succeed; the row is still written so the volume is visible.
    """
    tenant = await _lock_tenant(db, tenant_id)
    if tenant is None:
        return False
    await ensure_owner_verified(db, tenant)
    tier = effective(tenant)
    if tier.daily_outbound_calls is not None:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        used = await _usage_quantity(db, tenant_id, RING_LEG_METRIC, start, end)
        if used >= tier.daily_outbound_calls:
            return False
    db.add(
        UsageEvent(
            tenant_id=tenant_id,
            metric=RING_LEG_METRIC,
            quantity=1,
            occurred_at=datetime.now(UTC),
            extra={"kind": "ring"},
        )
    )
    await db.flush()
    return True


async def ensure_can_create(db: AsyncSession, tenant_id: uuid.UUID, resource: str) -> None:
    """Refuses creating one more `resource` when the plan's count is reached."""
    tenant = await _lock_tenant(db, tenant_id)
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


def _period_token(period: Period) -> str:
    if period == "lifetime":
        return "lifetime"
    start, _end = _window(period)
    return start.strftime("%Y-%m")


def _overage_key(tenant_id: uuid.UUID, token: str, suffix: str = "") -> str:
    key = f"overage:{tenant_id}:{token}{suffix}"
    return key[:64]


async def sync_overage_invoice(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Writes the unpaid usage past the allowance onto one pending invoice per period.

    `allow_overage` is what lets the workspace keep going. This is what makes
    that extra usage billable. Pay-as-you-go is charged from the wallet instead,
    and a plan with no published overage price (Free minutes, Enterprise) is
    not invoiced. Calling this again updates the open invoice; a paid one is
    left alone and any further usage becomes a new invoice. Returns the paise
    still unpaid.
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not allows_overage(tenant):
        return 0
    tier = effective(tenant)
    if not tier.is_limited or tier.code == PAYG.code:
        return 0

    from app.services import wallet

    owed_by_period: dict[str, tuple[int, list[str]]] = {}

    def add(period: Period, paise: int, part: str) -> None:
        if paise <= 0:
            return
        token = _period_token(period)
        total, parts = owed_by_period.get(token, (0, []))
        owed_by_period[token] = (total + paise, [*parts, part])

    if tier.ai_minutes is not None and tier.overage_per_min_inr:
        used = await billable_minutes_used(db, tenant.id, tier.ai_minutes_period)
        extra = max(used - tier.ai_minutes, 0)
        add(
            tier.ai_minutes_period,
            extra * int(round(tier.overage_per_min_inr * 100)),
            f"{extra} AI min",
        )
    message_meters = (
        (
            tier.whatsapp_messages,
            tier.whatsapp_period,
            await whatsapp_used(db, tenant.id, tier.whatsapp_period),
            wallet.WHATSAPP_MESSAGE_PAISE,
            "WhatsApp",
        ),
        (
            tier.sms_messages,
            tier.sms_period,
            await _usage_quantity(db, tenant.id, "sms", *_window(tier.sms_period)),
            wallet.SMS_PAISE,
            "SMS",
        ),
        (
            tier.email_messages,
            tier.email_period,
            await _usage_quantity(db, tenant.id, "email", *_window(tier.email_period)),
            wallet.EMAIL_PAISE,
            "email",
        ),
        (
            tier.social_messages,
            tier.social_period,
            await _usage_quantity(db, tenant.id, "social", *_window(tier.social_period)),
            wallet.SOCIAL_PAISE,
            "Instagram/Messenger",
        ),
    )
    for limit, period, used, rate, label in message_meters:
        if limit is None:
            continue
        extra = max(used - limit, 0)
        add(period, extra * rate, f"{extra} {label}")

    prefix = f"overage:{tenant.id}:"
    rows = (
        (
            await db.execute(
                select(Invoice).where(
                    Invoice.tenant_id == tenant.id,
                    Invoice.provider_payment_id.like(f"{prefix}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    by_token: dict[str, list[Invoice]] = {}
    for row in rows:
        rest = (row.provider_payment_id or "")[len(prefix):]
        token = rest.split(":", 1)[0]
        by_token.setdefault(token, []).append(row)
    for token in owed_by_period:
        by_token.setdefault(token, [])

    unpaid = 0
    now = datetime.now(UTC)
    for token, existing in by_token.items():
        owed, parts = owed_by_period.get(token, (0, []))
        paid = sum(
            row.amount_paise for row in existing if row.status == InvoiceStatus.PAID
        )
        pending = next((row for row in existing if row.status == InvoiceStatus.PENDING), None)
        gap = owed - paid
        if gap <= 0:
            if pending is not None:
                await db.delete(pending)
            continue
        description = f"Usage past the {tier.name} allowance ({token}): {', '.join(parts)}"[:255]
        if pending is not None:
            pending.amount_paise = gap
            pending.description = description
        else:
            base = _overage_key(tenant.id, token)
            taken = {row.provider_payment_id for row in existing}
            key = base if base not in taken else _overage_key(tenant.id, token, f":{len(existing) + 1}")
            db.add(
                Invoice(
                    tenant_id=tenant.id,
                    provider_payment_id=key,
                    description=description,
                    amount_paise=gap,
                    currency="INR",
                    status=InvoiceStatus.PENDING,
                    issued_at=now,
                )
            )
        unpaid += gap
    await db.flush()
    return unpaid


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
    overage_paise = await sync_overage_invoice(db, tenant.id)
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
        _meter(
            "sms",
            "SMS segments",
            await _usage_quantity(db, tenant.id, "sms", *_window(tier.sms_period)),
            tier.sms_messages,
            tier.sms_period,
            "sms",
        ),
        _meter(
            "email",
            "Emails",
            await _usage_quantity(db, tenant.id, "email", *_window(tier.email_period)),
            tier.email_messages,
            tier.email_period,
            "email",
        ),
        _meter(
            "social",
            "Instagram and Messenger",
            await _usage_quantity(db, tenant.id, "social", *_window(tier.social_period)),
            tier.social_messages,
            tier.social_period,
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
        "overage_paise": overage_paise,
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
    "claim_ring_leg",
    "ensure_can_create",
    "ensure_feature",
    "ensure_inbound_allowed",
    "ensure_minutes_available",
    "ensure_outbound_allowed",
    "ensure_owner_verified",
    "ensure_platform_number_purchase",
    "claim_whatsapp_send",
    "ensure_email_available",
    "ensure_social_available",
    "ensure_sms_available",
    "ensure_whatsapp_available",
    "release_whatsapp_claim",
    "reserve_email",
    "reserve_social",
    "reserve_sms",
    "entitlements",
    "sync_overage_invoice",
    "minutes_remaining",
    "public_catalogue",
    "tier_for_plan_code",
]
