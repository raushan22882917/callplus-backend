"""Plan tiers: the free tier's limits and the paid tiers' allowances are enforced.

The point of these tests is that a limit holds where the money is spent: a free
workspace cannot get past 10 AI call minutes, cannot out-create its plan, and
cannot raise its own limits.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1 import payments as payments_module
from app.core.config import get_settings
from app.services import cost_controls as cc
from app.services import plans
from app.tools.context import ToolContext
from app.tools.handlers import send_whatsapp_message
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    PhoneNumber,
    Plan,
    Subscription,
    SubscriptionStatus,
    Tenant,
    User,
    WhatsAppAccount,
)
from packages.database.models.platform import UsageEvent
from packages.database.session import session_scope
from tests.test_call_control import _auth, _internal_headers, _signup

# ── helpers ──────────────────────────────────────────────────────────────────


async def _tenant(db: AsyncSession, slug: str, tier: str = "free", **overrides) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, plan_tier=tier, plan_overrides=overrides)
    db.add(tenant)
    await db.commit()
    return tenant


async def _agent(db: AsyncSession, tenant: Tenant) -> AIAgent:
    agent = AIAgent(tenant_id=tenant.id, name="Bot")
    db.add(agent)
    await db.commit()
    return agent


async def _call(
    db: AsyncSession,
    tenant: Tenant,
    agent: AIAgent | None,
    seconds: int | None,
    *,
    outbound: bool = False,
    status: CallStatus = CallStatus.COMPLETED,
    started_ago: timedelta = timedelta(minutes=5),
) -> Call:
    call = Call(
        tenant_id=tenant.id,
        ai_agent_id=agent.id if agent else None,
        direction=CallDirection.OUTBOUND if outbound else CallDirection.INBOUND,
        status=status,
        started_at=datetime.now(UTC) - started_ago,
        duration_seconds=seconds,
    )
    db.add(call)
    await db.commit()
    return call


async def _on_tier(db: AsyncSession, slug: str, tier: str) -> uuid.UUID:
    row = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
    row.plan_tier = tier
    await db.commit()
    return row.id


# ── the catalogue ────────────────────────────────────────────────────────────


def test_free_tier_is_ten_minutes_in_total_with_a_short_call_cap() -> None:
    free = plans.TIERS["free"]
    assert free.ai_minutes == 10 and free.ai_minutes_period == "lifetime"
    assert free.max_call_seconds == 300
    assert free.campaigns is False and free.recording is False
    assert (free.agents, free.phone_numbers, free.knowledge_documents) == (1, 1, 5)


def test_paid_tiers_grow_and_are_priced_above_the_cost_of_serving_a_minute() -> None:
    order = [plans.TIERS[c] for c in ("free", "starter", "growth", "scale")]
    for smaller, bigger in zip(order, order[1:], strict=False):
        assert bigger.ai_minutes > smaller.ai_minutes
        assert bigger.price_month_inr > smaller.price_month_inr
        assert bigger.max_call_seconds >= smaller.max_call_seconds
    # scripts/cost_model.py: an AI minute costs about INR 4.4 blended (6.6 outbound).
    # Every paid plan must charge clearly more per included minute than that.
    for tier in order[1:]:
        assert tier.price_month_inr / tier.ai_minutes > 6.6 * 1.2, tier.code


def test_legacy_and_enterprise_are_unlimited_and_plan_codes_map_to_tiers() -> None:
    for code in ("legacy", "enterprise"):
        tier = plans.TIERS[code]
        assert tier.ai_minutes is None and tier.agents is None
    assert plans.tier_for_plan_code("growth") == "growth"
    assert plans.tier_for_plan_code("scale_annual") == "scale"
    assert plans.tier_for_plan_code("free") is None
    assert plans.tier_for_plan_code("nonsense") is None


async def test_overrides_add_minutes_and_reject_typos(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, "ovr", "free", bonus_minutes=20, recording=True)
    tier = plans.effective(tenant)
    assert tier.ai_minutes == 30 and tier.recording is True and tier.campaigns is False
    assert plans.sanitize_overrides({"bonus_minutes": 5, "allow_overage": True}) == {
        "bonus_minutes": 5,
        "allow_overage": True,
    }
    for bad in ({"bonus_minute": 5}, {"bonus_minutes": "5"}, {"recording": 1}, {"agents": -1}):
        with pytest.raises(ValueError):
            plans.sanitize_overrides(bad)


# ── minutes: metering, the pool, the clamp ───────────────────────────────────


async def test_minutes_are_rounded_up_per_call_like_the_carrier_bills(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, "round")
    agent = await _agent(db_session, tenant)
    for seconds in (5, 61, 60, None, 0):  # 1 + 2 + 1 minutes; unanswered calls cost nothing
        await _call(db_session, tenant, agent, seconds)
    assert await plans.billable_minutes_used(db_session, tenant.id, "lifetime") == 4
    # human-handled calls (no AI agent) are not AI minutes
    await _call(db_session, tenant, None, 600)
    assert await plans.billable_minutes_used(db_session, tenant.id, "lifetime") == 4


async def test_call_config_clamps_the_call_to_what_is_left_of_the_free_minutes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant = await _tenant(db_session, "clamp")
    agent = await _agent(db_session, tenant)
    await _call(db_session, tenant, agent, 7 * 60)  # 7 of 10 minutes gone, 3 left
    live = await _call(db_session, tenant, agent, None, status=CallStatus.IN_PROGRESS)

    resp = await client.get(f"/api/v1/internal/calls/{live.id}/config", headers=_internal_headers())
    assert resp.status_code == 200, resp.text
    # agent default is 300s, plan per-call cap 300s, but only 3 minutes remain
    assert resp.json()["max_call_duration_seconds"] == 180


async def test_a_full_pool_cannot_run_past_ten_minutes_even_with_a_long_agent_cap(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant = await _tenant(db_session, "longcap")
    agent = await _agent(db_session, tenant)
    agent.max_call_duration_seconds = 3600  # the tenant tried to allow hour-long calls
    await db_session.commit()
    live = await _call(db_session, tenant, agent, None, status=CallStatus.IN_PROGRESS)

    resp = await client.get(f"/api/v1/internal/calls/{live.id}/config", headers=_internal_headers())
    assert resp.json()["max_call_duration_seconds"] == 300  # the free per-call cap


async def test_paid_plan_call_cap_and_legacy_are_not_clamped(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    for slug, tier, expected in (("starter-cfg", "starter", 900), ("legacy-cfg", "legacy", 3600)):
        tenant = await _tenant(db_session, slug, tier)
        agent = await _agent(db_session, tenant)
        agent.max_call_duration_seconds = 3600
        await db_session.commit()
        live = await _call(db_session, tenant, agent, None, status=CallStatus.IN_PROGRESS)
        resp = await client.get(
            f"/api/v1/internal/calls/{live.id}/config", headers=_internal_headers()
        )
        assert resp.json()["max_call_duration_seconds"] == expected, tier


async def test_turn_endpoint_hangs_up_at_the_plan_cap(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant = await _tenant(db_session, "turncap")
    agent = await _agent(db_session, tenant)
    await _call(db_session, tenant, agent, 8 * 60)  # 2 minutes left
    live = await _call(
        db_session,
        tenant,
        agent,
        None,
        status=CallStatus.IN_PROGRESS,
        started_ago=timedelta(seconds=125),
    )
    resp = await client.post(
        f"/api/v1/internal/calls/{live.id}/turn",
        headers=_internal_headers(),
        json={"transcript": "hello"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "hangup"
    assert resp.json()["reason"] == "max_duration_exceeded"


# ── blocking new calls ───────────────────────────────────────────────────────


async def test_outbound_is_refused_once_the_free_minutes_are_gone(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, "outdone")
    agent = await _agent(db_session, tenant)
    await cc.guard_outbound(db_session, tenant.id)  # fresh: allowed

    await _call(db_session, tenant, agent, 10 * 60)
    with pytest.raises(plans.PlanLimitError) as err:
        await cc.guard_outbound(db_session, tenant.id)
    assert err.value.code == "plan_minutes_exhausted" and err.value.http_status == 402
    # it is a SpendGuardError, so every existing outbound path already handles it
    assert isinstance(err.value, cc.SpendGuardError)


async def test_free_outbound_daily_cap_and_one_call_at_a_time(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, "daily")
    agent = await _agent(db_session, tenant)

    await _call(db_session, tenant, agent, None, outbound=True, status=CallStatus.IN_PROGRESS)
    with pytest.raises(plans.PlanLimitError) as err:
        await cc.guard_outbound(db_session, tenant.id)
    assert err.value.code == "plan_concurrency" and err.value.http_status == 429

    other = await _tenant(db_session, "daily2")
    other_agent = await _agent(db_session, other)
    for _ in range(5):  # five 1-second calls: 5 outbound today, but only 5 minutes used
        await _call(db_session, other, other_agent, 1, outbound=True)
    with pytest.raises(plans.PlanLimitError) as err:
        await cc.guard_outbound(db_session, other.id)
    assert err.value.code == "plan_daily_outbound"


async def test_inbound_call_gets_a_polite_message_when_minutes_are_gone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant = await _tenant(db_session, "inbound")
    agent = await _agent(db_session, tenant)
    db_session.add(
        PhoneNumber(tenant_id=tenant.id, e164_number="+15557770001", ai_agent_id=agent.id)
    )
    await db_session.commit()

    ok = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15557770001", "From": "+15558880001", "CallSid": "CA1"},
    )
    assert ok.status_code == 200 and "<Stream" in ok.text  # connected to the AI

    await _call(db_session, tenant, agent, 10 * 60)
    refused = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15557770001", "From": "+15558880002", "CallSid": "CA2"},
    )
    assert refused.status_code == 200
    assert "<Hangup" in refused.text and "<Stream" not in refused.text
    assert "can't take your call" in refused.text
    # the caller, who is the business's customer, must not hear about plans or billing
    assert "plan" not in refused.text.lower() and "free" not in refused.text.lower()


# ── counts and features, through the API ─────────────────────────────────────


async def test_free_workspace_cannot_out_create_its_plan(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="counts", email="owner@counts.com")
    await _on_tier(db_session, "counts", "free")
    h = _auth(tokens)

    first = await client.post("/api/v1/ai-agents", headers=h, json={"name": "One"})
    assert first.status_code == 201, first.text
    second = await client.post("/api/v1/ai-agents", headers=h, json={"name": "Two"})
    assert second.status_code == 402
    body = second.json()
    assert body["code"] == "plan_agents_limit" and body["tier"] == "free"
    assert body["upgrade_path"] == "/billing" and "Upgrade" in body["detail"]

    for i in range(5):
        r = await client.post(
            "/api/v1/knowledge/documents", headers=h, json={"title": f"D{i}", "content": "text"}
        )
        assert r.status_code == 201, r.text
    over = await client.post(
        "/api/v1/knowledge/documents", headers=h, json={"title": "D6", "content": "text"}
    )
    assert over.status_code == 402 and over.json()["code"] == "plan_knowledge_documents_limit"

    key = {"name": "k", "mode": "test", "scopes": ["calls:read"]}
    assert (await client.post("/api/v1/developer/api-keys", headers=h, json=key)).status_code == 201
    assert (await client.post("/api/v1/developer/api-keys", headers=h, json=key)).status_code == 402


async def test_free_workspace_has_no_campaigns_or_recording_and_two_seats(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="gates", email="owner@gates.com")
    await _on_tier(db_session, "gates", "free")
    h = _auth(tokens)

    camp = await client.post(
        "/api/v1/campaigns", headers=h, json={"name": "Blast", "channel": "voice"}
    )
    assert camp.status_code == 402 and camp.json()["code"] == "plan_campaigns_unavailable"

    rec = await client.patch("/api/v1/tenants/me", headers=h, json={"call_recording_enabled": True})
    assert rec.status_code == 402 and rec.json()["code"] == "plan_recording_unavailable"
    off = await client.patch(
        "/api/v1/tenants/me", headers=h, json={"call_recording_enabled": False}
    )
    assert off.status_code == 200  # turning it off is always fine

    invite = {"full_name": "A", "role_name": "AGENT"}
    one = await client.post(
        "/api/v1/users/invite", headers=h, json={**invite, "email": "a@gates.com"}
    )
    assert one.status_code == 201, one.text  # owner + 1 = the free plan's 2 seats
    two = await client.post(
        "/api/v1/users/invite", headers=h, json={**invite, "email": "b@gates.com"}
    )
    assert two.status_code == 402 and two.json()["code"] == "plan_team_members_limit"


async def test_upgrading_the_tier_lifts_the_limit_immediately(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="lift", email="owner@lift.com")
    await _on_tier(db_session, "lift", "free")
    h = _auth(tokens)
    assert (
        await client.post("/api/v1/ai-agents", headers=h, json={"name": "A"})
    ).status_code == 201
    assert (
        await client.post("/api/v1/ai-agents", headers=h, json={"name": "B"})
    ).status_code == 402
    await _on_tier(db_session, "lift", "starter")
    assert (
        await client.post("/api/v1/ai-agents", headers=h, json={"name": "B"})
    ).status_code == 201


async def test_legacy_workspaces_are_never_limited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="oldco", email="owner@oldco.com")
    h = _auth(tokens)  # the test suite signs workspaces up as 'legacy'
    for i in range(4):
        r = await client.post("/api/v1/ai-agents", headers=h, json={"name": f"A{i}"})
        assert r.status_code == 201, r.text
    camp = await client.post(
        "/api/v1/campaigns", headers=h, json={"name": "Ok", "channel": "voice"}
    )
    assert camp.status_code == 201


async def test_signup_uses_the_configured_starting_tier(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "signup_plan_tier", "free")
    await _signup(client, slug="newfree", email="owner@newfree.com")
    monkeypatch.setattr(get_settings(), "signup_plan_tier", "bogus")  # bad config -> safe default
    await _signup(client, slug="newbogus", email="owner@newbogus.com")
    async with session_scope() as db:
        rows = {t.slug: t.plan_tier for t in (await db.execute(select(Tenant))).scalars()}
    assert rows["newfree"] == "free" and rows["newbogus"] == "free"


# ── WhatsApp: metering and cap ───────────────────────────────────────────────


async def test_whatsapp_sends_are_metered_and_capped(db_session: AsyncSession) -> None:
    from app.integrations.whatsapp import record_usage

    tenant = await _tenant(db_session, "wa")
    await record_usage(tenant.id)  # what a real send does after Meta accepts the message
    assert await plans.whatsapp_used(db_session, tenant.id, "lifetime") == 1

    db_session.add(
        UsageEvent(
            tenant_id=tenant.id,
            metric="whatsapp_messages",
            quantity=48,
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await plans.ensure_whatsapp_available(db_session, tenant.id)  # 49 of 50 used: one more fits
    with pytest.raises(plans.PlanLimitError) as err:
        await plans.ensure_whatsapp_available(db_session, tenant.id, count=2)
    assert err.value.code == "plan_whatsapp_exhausted"


async def test_ai_tool_stops_sending_whatsapp_when_the_allowance_is_gone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _tenant(db_session, "watool")
    db_session.add(
        WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id="123",
            waba_id="w",
            display_phone_number="+15550001",
            encrypted_access_token="x",
        )
    )
    db_session.add(
        UsageEvent(
            tenant_id=tenant.id,
            metric="whatsapp_messages",
            quantity=50,
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    sent: list = []

    async def fake_send(*args, **kwargs):
        sent.append(kwargs)
        return {"messages": [{"id": "wamid"}]}

    monkeypatch.setattr("app.tools.handlers.send_text_message", fake_send)
    ctx = ToolContext(
        db=db_session,
        tenant_id=tenant.id,
        agent_id=None,
        call_id=None,
        conversation_id=None,
        customer_id=None,
    )
    result = await send_whatsapp_message(ctx, phone="+919800000000", message="hi")
    assert "used up" in result["error"] and sent == []


# ── upgrade / downgrade through Razorpay ─────────────────────────────────────


async def _subscribe(db: AsyncSession, tenant: Tenant, code: str) -> Subscription:
    plan = Plan(
        code=code,
        name=code.title(),
        price_paise=100,
        provider_plan_id=f"plan_{code}",
    )
    db.add(plan)
    await db.flush()
    sub = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        provider_subscription_id=f"sub_{code}_{tenant.slug}",
        status=SubscriptionStatus.CREATED,
    )
    db.add(sub)
    await db.commit()
    return sub


async def _event(db: AsyncSession, sub: Subscription, event: str) -> None:
    await payments_module._handle_subscription_event(
        db,
        event=event,
        subscription_entity={"id": sub.provider_subscription_id},
        payment_entity={},
    )
    await db.commit()


async def test_subscription_events_move_the_tenant_between_tiers(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, "razor")
    sub = await _subscribe(db_session, tenant, "growth")

    await _event(db_session, sub, "subscription.activated")
    await db_session.refresh(tenant)
    assert tenant.plan_tier == "growth"

    await _event(db_session, sub, "subscription.halted")  # payment failing: keep the tier
    await db_session.refresh(tenant)
    assert tenant.plan_tier == "growth"

    await _event(db_session, sub, "subscription.cancelled")
    await db_session.refresh(tenant)
    assert tenant.plan_tier == "free"


async def test_a_stale_subscription_does_not_downgrade_a_hand_granted_tier(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, "deal", "enterprise")
    sub = await _subscribe(db_session, tenant, "growth")
    await _event(db_session, sub, "subscription.cancelled")
    await db_session.refresh(tenant)
    assert tenant.plan_tier == "enterprise"


# ── who may change a plan ────────────────────────────────────────────────────


async def test_only_a_platform_admin_can_change_a_plan(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _signup(client, slug="victim", email="owner@victim.com")
    victim_id = await _on_tier(db_session, "victim", "free")

    # a tenant cannot reach its own entitlements, however it asks
    attempt = await client.patch(
        "/api/v1/tenants/me", headers=_auth(owner), json={"plan_tier": "scale"}
    )
    assert attempt.status_code in (200, 422)
    forbidden = await client.put(
        f"/api/v1/platform/admin/tenants/{victim_id}/plan", headers=_auth(owner), json={"tier": "scale"}
    )
    assert forbidden.status_code == 403
    assert (await db_session.get(Tenant, victim_id)).plan_tier == "free"

    admin = await _signup(client, slug="platform", email="admin@platform.com")
    row = (
        await db_session.execute(select(User).where(User.email == "admin@platform.com"))
    ).scalar_one()
    row.is_platform_admin = True
    await db_session.commit()

    ok = await client.put(
        f"/api/v1/platform/admin/tenants/{victim_id}/plan",
        headers=_auth(admin),
        json={"tier": "free", "overrides": {"bonus_minutes": 15}, "reason": "design partner"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["plan_tier"] == "free"
    db_session.expire_all()
    assert plans.effective(await db_session.get(Tenant, victim_id)).ai_minutes == 25

    bad_tier = await client.put(
        f"/api/v1/platform/admin/tenants/{victim_id}/plan", headers=_auth(admin), json={"tier": "platinum"}
    )
    assert bad_tier.status_code == 400
    bad_override = await client.put(
        f"/api/v1/platform/admin/tenants/{victim_id}/plan",
        headers=_auth(admin),
        json={"tier": "free", "overrides": {"bonus_minute": 1}},
    )
    assert bad_override.status_code == 400


# ── what the Billing page sees ───────────────────────────────────────────────


async def test_entitlements_and_catalogue_endpoints(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="meters", email="owner@meters.com")
    tenant_id = await _on_tier(db_session, "meters", "free")
    tenant = await db_session.get(Tenant, tenant_id)
    agent = await _agent(db_session, tenant)
    await _call(db_session, tenant, agent, 150)  # 3 billable minutes

    resp = await client.get("/api/v1/billing/entitlements", headers=_auth(tokens))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "free" and body["is_free"] is True
    assert body["max_call_seconds"] == 300 and body["campaigns"] is False
    minutes = next(m for m in body["meters"] if m["key"] == "ai_minutes")
    assert (minutes["used"], minutes["limit"], minutes["remaining"], minutes["period"]) == (
        3,
        10,
        7,
        "lifetime",
    )
    agents = next(m for m in body["meters"] if m["key"] == "agents")
    assert (agents["used"], agents["limit"]) == (1, 1)

    cat = await client.get("/api/v1/billing/catalogue", headers=_auth(tokens))
    assert [t["code"] for t in cat.json()] == ["free", "starter", "growth", "scale", "enterprise"]
    assert cat.json()[1]["price_month_inr"] == 2999
