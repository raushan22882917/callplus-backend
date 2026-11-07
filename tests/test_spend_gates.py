"""Spend gates that sit off the straight AI-call path.

A free workspace used to be able to buy a platform number, reach the agent
through a menu after its minutes were gone, send SMS without a cap, and ring
a queue of people on our Twilio account. These tests pin the rules, then run
a thousand mixed decisions at once against a real Postgres.
"""

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.core.config import get_settings
from app.services import number_lifecycle, plans
from app.services.telephony_plus import (
    InboundRoute,
    ring_queue_members,
    route_can_reach_ai,
    route_reaches_ai,
)
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Customer,
    IvrMenu,
    MembershipStatus,
    NumberRouting,
    PhoneNumber,
    Role,
    Tenant,
    TenantUser,
    User,
)
from packages.database.models.platform import UsageEvent
from packages.database.models.telephony_plus import CallQueue
from packages.database.rls import apply_rls
from packages.database.session import get_engine, session_scope

# The allowances, written here on purpose so a change in plans.py has to be
# matched deliberately. None means the plan does not cap that meter.
SPEC = {
    "free": {"minutes": 10, "sms": 20, "rings": 5, "mail": 30, "social": 50},
    "starter": {"minutes": 300, "sms": 300, "rings": None, "mail": 500, "social": 1000},
    "growth": {"minutes": 1500, "sms": 1500, "rings": None, "mail": 2000, "social": 5000},
    "scale": {"minutes": 6000, "sms": 8000, "rings": None, "mail": 10000, "social": 25000},
    "enterprise": {"minutes": None, "sms": None, "rings": None, "mail": None, "social": None},
    "legacy": {"minutes": None, "sms": None, "rings": None, "mail": None, "social": None},
}
KINDS = ("minutes", "sms", "rings", "mail", "social")


def test_the_enforced_allowances_match_the_spec() -> None:
    for code, spec in SPEC.items():
        tier = plans.TIERS[code]
        assert tier.ai_minutes == spec["minutes"], code
        assert tier.sms_messages == spec["sms"], code
        assert tier.email_messages == spec["mail"], code
        assert tier.social_messages == spec["social"], code
        assert tier.daily_outbound_calls == spec["rings"], code


def test_a_menu_that_offers_the_agent_counts_as_an_ai_call() -> None:
    menu = SimpleNamespace(
        after_hours_action=None,
        options={"1": {"action": "hangup"}, "2": {"action": "ai_agent", "target_id": "x"}},
    )
    assert route_can_reach_ai(InboundRoute("ivr", menu=menu)) is True
    human = SimpleNamespace(after_hours_action=None, options={"1": {"action": "hangup"}})
    assert route_can_reach_ai(InboundRoute("ivr", menu=human)) is False
    queue = SimpleNamespace(overflow_action={"action": "ai_agent", "target_id": "x"})
    assert route_can_reach_ai(InboundRoute("queue", queue=queue)) is True
    assert route_can_reach_ai(InboundRoute("ai_agent", ai_agent_id=uuid.uuid4())) is True
    assert route_can_reach_ai(None) is False


async def test_a_nested_submenu_counts_before_the_caller_presses(db_session: AsyncSession) -> None:
    tenant = Tenant(name="Nest", slug="nest-menu", plan_tier="free")
    db_session.add(tenant)
    await db_session.flush()
    child = IvrMenu(
        tenant_id=tenant.id,
        name="Inner",
        greeting_text="Inner",
        options={"1": {"action": "ai_agent", "target_id": str(uuid.uuid4())}},
    )
    db_session.add(child)
    await db_session.flush()
    parent = IvrMenu(
        tenant_id=tenant.id,
        name="Outer",
        greeting_text="Outer",
        options={"1": {"action": "submenu", "target_id": str(child.id)}},
    )
    db_session.add(parent)
    await db_session.commit()
    route = InboundRoute("ivr", menu=parent)
    assert route_can_reach_ai(route) is False
    assert await route_reaches_ai(db_session, route) is True


def _cases(n: int = 1000) -> list[dict]:
    """n decisions. Every 20th is an unverified free owner. The rest are a
    seeded mix of tier, meter and amount already used."""
    rng = random.Random(20261107)
    tiers = list(SPEC)
    cases = []
    for i in range(n):
        if i % 20 == 0:
            cases.append(
                {"i": i, "kind": "email", "tier": "free", "used": 0, "expect": "email_unverified"}
            )
            continue
        tier = tiers[i % len(tiers)]
        kind = KINDS[i % len(KINDS)]
        limit = SPEC[tier][kind]
        if limit is None:
            used = rng.choice((0, 1, 10_000))
            expect = "allowed"
        else:
            used = rng.randrange(0, limit + 6)
            expect = "allowed" if used < limit else {
                "minutes": "plan_minutes_exhausted",
                "sms": "plan_sms_exhausted",
                "rings": "plan_daily_outbound",
                "mail": "plan_email_exhausted",
                "social": "plan_social_exhausted",
            }[kind]
        cases.append({"i": i, "kind": kind, "tier": tier, "used": used, "expect": expect})
    return cases


async def _owner(db: AsyncSession, tenant: Tenant, *, verified: bool) -> None:
    role = (
        await db.execute(select(Role).where(Role.name == "OWNER", Role.tenant_id.is_(None)))
    ).scalar_one()
    user = User(
        email=f"{tenant.slug}@example.com",
        hashed_password="not-used",
        full_name="Owner",
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(user)
    await db.flush()
    db.add(
        TenantUser(
            tenant_id=tenant.id,
            user_id=user.id,
            role_id=role.id,
            status=MembershipStatus.ACTIVE,
        )
    )


async def test_one_thousand_mixed_decisions_at_once(db_session: AsyncSession) -> None:
    cases = _cases(1000)
    tenants: list[Tenant] = []
    for case in cases:
        tenant = Tenant(name=f"M{case['i']}", slug=f"mix-{case['i']}", plan_tier=case["tier"])
        db_session.add(tenant)
        tenants.append(tenant)
    await db_session.flush()

    for case, tenant in zip(cases, tenants, strict=True):
        case["tenant_id"] = tenant.id
        if case["kind"] == "email":
            await _owner(db_session, tenant, verified=False)
            continue
        if case["kind"] == "minutes" and case["used"] > 0:
            agent = AIAgent(tenant_id=tenant.id, name="Bot")
            db_session.add(agent)
            await db_session.flush()
            db_session.add(
                Call(
                    tenant_id=tenant.id,
                    ai_agent_id=agent.id,
                    direction=CallDirection.INBOUND,
                    status=CallStatus.COMPLETED,
                    duration_seconds=case["used"] * 60,
                    started_at=datetime.now(UTC),
                )
            )
        elif case["kind"] == "sms" and case["used"] > 0:
            db_session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    metric="sms",
                    quantity=case["used"],
                    occurred_at=datetime.now(UTC),
                )
            )
        elif case["kind"] == "social" and case["used"] > 0:
            db_session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    metric="social",
                    quantity=case["used"],
                    occurred_at=datetime.now(UTC),
                )
            )
        elif case["kind"] == "mail" and case["used"] > 0:
            db_session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    metric="email",
                    quantity=case["used"],
                    occurred_at=datetime.now(UTC),
                )
            )
        elif case["kind"] == "rings" and case["used"] > 0:
            db_session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    metric=plans.RING_LEG_METRIC,
                    quantity=case["used"],
                    occurred_at=datetime.now(UTC),
                )
            )
    await db_session.commit()

    sem = asyncio.Semaphore(8)

    async def run(case: dict) -> str:
        async with sem:
            async with session_scope() as db:
                try:
                    if case["kind"] == "email":
                        await plans.ensure_inbound_allowed(db, case["tenant_id"])
                    elif case["kind"] == "minutes":
                        await plans.ensure_minutes_available(db, case["tenant_id"])
                    elif case["kind"] == "sms":
                        await plans.ensure_sms_available(db, case["tenant_id"], 1)
                    elif case["kind"] == "mail":
                        await plans.ensure_email_available(db, case["tenant_id"])
                    elif case["kind"] == "social":
                        await plans.ensure_social_available(db, case["tenant_id"])
                    else:
                        ok = await plans.claim_ring_leg(db, case["tenant_id"])
                        await db.rollback()
                        return "allowed" if ok else "plan_daily_outbound"
                except plans.PlanLimitError as exc:
                    await db.rollback()
                    return exc.code
                return "allowed"

    results = await asyncio.gather(*(run(case) for case in cases))
    bad = [
        (case["i"], case["tier"], case["kind"], case["used"], case["expect"], got)
        for case, got in zip(cases, results, strict=True)
        if got != case["expect"]
    ]
    assert not bad, bad[:8]


async def test_one_live_call_is_admitted_when_many_arrive_together(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(name="Burst", slug="burst", plan_tier="free")
    db_session.add(tenant)
    await db_session.flush()
    agent = AIAgent(tenant_id=tenant.id, name="Bot")
    db_session.add(agent)
    await db_session.commit()

    sem = asyncio.Semaphore(8)

    async def once() -> bool:
        async with sem:
            async with session_scope() as db:
                try:
                    await plans.ensure_inbound_allowed(db, tenant.id)
                except plans.PlanLimitError:
                    await db.rollback()
                    return False
                db.add(
                    Call(
                        tenant_id=tenant.id,
                        ai_agent_id=agent.id,
                        direction=CallDirection.INBOUND,
                        status=CallStatus.RINGING,
                        started_at=datetime.now(UTC),
                    )
                )
                await db.commit()
                return True

    admitted = await asyncio.gather(*(once() for _ in range(32)))
    assert sum(admitted) == 1


async def test_parallel_sms_cannot_share_the_last_segment(db_session: AsyncSession) -> None:
    tenant = Tenant(name="Sms", slug="sms-race", plan_tier="free")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        UsageEvent(
            tenant_id=tenant.id, metric="sms", quantity=19, occurred_at=datetime.now(UTC)
        )
    )
    await db_session.commit()

    sem = asyncio.Semaphore(8)

    async def once() -> bool:
        async with sem:
            async with session_scope() as db:
                try:
                    await plans.reserve_sms(db, tenant.id, 1)
                except plans.PlanLimitError:
                    await db.rollback()
                    return False
                await db.commit()
                return True

    reserved = await asyncio.gather(*(once() for _ in range(16)))
    assert sum(reserved) == 1


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug,
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"{slug}@example.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _as_free(db: AsyncSession, slug: str, *, verified: bool) -> Tenant:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
    tenant.plan_tier = "free"
    user = (await db.execute(select(User).where(User.email == f"{slug}@example.com"))).scalar_one()
    user.email_verified_at = datetime.now(UTC) if verified else None
    await db.commit()
    return tenant


async def test_inbound_through_a_menu_stops_when_the_minutes_are_gone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, "menu-clinic")
    tenant = await _as_free(db_session, "menu-clinic", verified=True)
    agent = AIAgent(tenant_id=tenant.id, name="Bot")
    db_session.add(agent)
    await db_session.flush()
    db_session.add(
        Call(
            tenant_id=tenant.id,
            ai_agent_id=agent.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            duration_seconds=10 * 60,
            started_at=datetime.now(UTC),
        )
    )
    menu = IvrMenu(
        tenant_id=tenant.id,
        name="Main",
        greeting_text="Press 1 for the assistant.",
        options={"1": {"action": "ai_agent", "target_id": str(agent.id)}},
    )
    db_session.add(menu)
    await db_session.flush()
    db_session.add(PhoneNumber(tenant_id=tenant.id, e164_number="+15551110001"))
    db_session.add(
        NumberRouting(
            tenant_id=tenant.id, e164_number="+15551110001", target_type="ivr", target_id=menu.id
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15551110001", "From": "+15552220002", "CallSid": "CAmenu"},
    )
    assert resp.status_code == 200
    assert "can't take your call" in resp.text
    assert "<Stream" not in resp.text


async def test_a_human_menu_is_not_blocked_by_spent_ai_minutes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, "human-menu")
    tenant = await _as_free(db_session, "human-menu", verified=True)
    agent = AIAgent(tenant_id=tenant.id, name="Bot")
    db_session.add(agent)
    await db_session.flush()
    db_session.add(
        Call(
            tenant_id=tenant.id,
            ai_agent_id=agent.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            duration_seconds=10 * 60,
            started_at=datetime.now(UTC),
        )
    )
    menu = IvrMenu(
        tenant_id=tenant.id,
        name="Hours",
        greeting_text="Press 1 to leave a message.",
        options={"1": {"action": "hangup"}},
    )
    db_session.add(menu)
    await db_session.flush()
    db_session.add(PhoneNumber(tenant_id=tenant.id, e164_number="+15551110002"))
    db_session.add(
        NumberRouting(
            tenant_id=tenant.id, e164_number="+15551110002", target_type="ivr", target_id=menu.id
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15551110002", "From": "+15552220003", "CallSid": "CAhuman"},
    )
    assert resp.status_code == 200
    assert "<Gather" in resp.text
    assert "<Stream" not in resp.text


async def test_unverified_owner_cannot_spend(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = await _signup(client, "unverified")
    await _as_free(db_session, "unverified", verified=False)
    agent = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"}
    )
    assert agent.status_code == 201, agent.text
    bought: list = []

    def purchase(**kwargs):
        bought.append(kwargs)
        return "PNshould-not"

    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.api.v1.agents.purchase_number", purchase)
    try:
        resp = await client.post(
            "/api/v1/phone-numbers/provision",
            headers=_auth(tokens),
            json={"phone_number": "+15551110009", "ai_agent_id": agent.json()["id"]},
        )
    finally:
        monkey.undo()
    assert resp.status_code == 403
    assert resp.json()["code"] == "email_unverified"
    assert bought == []


async def test_free_cannot_buy_a_platform_number_and_delete_releases_one_we_bought(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = await _signup(client, "numbers")
    tenant = await _as_free(db_session, "numbers", verified=True)
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"})
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]
    bought: list = []
    monkeypatch.setattr(
        "app.api.v1.agents.purchase_number", lambda **kw: bought.append(kw) or "PN1"
    )
    resp = await client.post(
        "/api/v1/phone-numbers/provision",
        headers=_auth(tokens),
        json={"phone_number": "+15551110003", "ai_agent_id": agent_id},
    )
    assert resp.status_code == 402
    assert resp.json()["code"] == "plan_platform_number"
    assert bought == []

    assigned = await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": "+15551110004", "ai_agent_id": agent_id},
    )
    assert assigned.status_code == 201, assigned.text

    released: list = []
    monkeypatch.setattr(
        "app.services.number_lifecycle.release_number", lambda **kw: released.append(kw)
    )
    number = PhoneNumber(
        tenant_id=tenant.id,
        e164_number="+15551110005",
        provider_sid="PNbought",
        platform_owned=True,
        ai_agent_id=uuid.UUID(agent_id),
    )
    db_session.add(number)
    await db_session.commit()
    # Free includes one number, and the assigned one already uses it. Delete
    # that one first (it is not platform-owned, so Twilio is not called).
    gone = await client.delete(
        f"/api/v1/phone-numbers/{assigned.json()['id']}", headers=_auth(tokens)
    )
    assert gone.status_code == 204
    assert released == []

    # The count is free again, but this row is one we bought. Removing it
    # releases it. The plan cap is not what this request is testing: delete
    # does not create a number.
    removed = await client.delete(f"/api/v1/phone-numbers/{number.id}", headers=_auth(tokens))
    assert removed.status_code == 204, removed.text
    assert released == [{"provider_sid": "PNbought", "credentials": released[0]["credentials"]}]
    # The request committed the delete on its own connection. Read it back
    # from a new session so this one does not return the row it still holds.
    async with session_scope() as fresh:
        still = await fresh.get(PhoneNumber, number.id)
    assert still is None


async def test_an_idle_free_number_is_released(db_session: AsyncSession, monkeypatch) -> None:
    released: list[str] = []
    monkeypatch.setattr(
        "app.services.number_lifecycle.release_number",
        lambda **kw: released.append(kw["provider_sid"]),
    )
    idle = Tenant(name="Idle", slug="idle-free", plan_tier="free")
    busy = Tenant(name="Busy", slug="busy-free", plan_tier="free")
    db_session.add_all([idle, busy])
    await db_session.flush()
    idle.created_at = datetime.now(UTC) - timedelta(days=30)
    busy.created_at = datetime.now(UTC) - timedelta(days=30)
    db_session.add_all(
        [
            PhoneNumber(
                tenant_id=idle.id,
                e164_number="+15551110006",
                provider_sid="PNidle",
                platform_owned=True,
            ),
            PhoneNumber(
                tenant_id=busy.id,
                e164_number="+15551110007",
                provider_sid="PNbusy",
                platform_owned=True,
            ),
        ]
    )
    await db_session.flush()
    agent = AIAgent(tenant_id=busy.id, name="Bot")
    db_session.add(agent)
    await db_session.flush()
    db_session.add(
        Call(
            tenant_id=busy.id,
            ai_agent_id=agent.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            duration_seconds=30,
            started_at=datetime.now(UTC) - timedelta(days=1),
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.commit()

    count = await number_lifecycle.release_idle_platform_numbers(db_session)
    assert count == 1
    assert released == ["PNidle"]
    left = (
        await db_session.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == idle.id))
    ).scalar_one_or_none()
    assert left is None
    kept = (
        await db_session.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == busy.id))
    ).scalar_one()
    assert kept.provider_sid == "PNbusy"


async def test_a_free_queue_rings_one_person(db_session: AsyncSession, monkeypatch) -> None:
    placed: list[str] = []
    monkeypatch.setattr(
        "app.services.telephony_plus.place_outbound_call",
        lambda **kw: placed.append(kw["to"]) or "CA1",
    )
    tenant = Tenant(name="Queue", slug="queue-free", plan_tier="free")
    db_session.add(tenant)
    await db_session.flush()
    call = Call(
        tenant_id=tenant.id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        to_number="+15551110008",
        from_number="+15552220008",
    )
    queue = CallQueue(
        tenant_id=tenant.id,
        name="Front",
        members=["+15550000001", "+15550000002", "+15550000003", "+15550000004"],
    )
    db_session.add_all([call, queue])
    await db_session.commit()

    started = await ring_queue_members(db_session, queue, call)
    assert started == 1
    assert placed == ["+15550000001"]


async def test_a_human_call_on_our_number_spends_the_minute_pool(db_session: AsyncSession) -> None:
    tenant = Tenant(name="Desk", slug="desk-free", plan_tier="free")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        PhoneNumber(
            tenant_id=tenant.id,
            e164_number="+15551110020",
            platform_owned=True,
        )
    )
    db_session.add(
        Call(
            tenant_id=tenant.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            to_number="+15551110020",
            from_number="+15552220020",
            duration_seconds=10 * 60,
            started_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    with pytest.raises(plans.PlanLimitError) as err:
        await plans.ensure_inbound_allowed(db_session, tenant.id)
    assert err.value.code == "plan_minutes_exhausted"


async def test_two_whatsapp_claims_cannot_share_the_last_message(db_session: AsyncSession) -> None:
    tenant = Tenant(name="Wa", slug="wa-race", plan_tier="free")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        UsageEvent(
            tenant_id=tenant.id,
            metric="whatsapp_messages",
            quantity=49,
            occurred_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    async def once() -> bool:
        try:
            event_id = await plans.claim_whatsapp_send(tenant.id)
        except plans.PlanLimitError:
            return False
        assert event_id is not None
        return True

    results = await asyncio.gather(*(once() for _ in range(8)))
    assert results.count(True) == 1


async def test_non_superuser_role_cannot_read_another_tenant(db_session: AsyncSession) -> None:
    """The production role is not a superuser, so row-level security applies.
    This connects as a role with the same restrictions and checks the fence."""
    a = Tenant(name="A", slug="rls-a", plan_tier="free")
    b = Tenant(name="B", slug="rls-b", plan_tier="free")
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add_all(
        [
            Customer(tenant_id=a.id, name="Ann", phone="+15551000001"),
            Customer(tenant_id=b.id, name="Ben", phone="+15551000002"),
        ]
    )
    await db_session.commit()

    role = "callcenter_rls_probe"
    password = "probe-pass-1"
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(apply_rls)

    import getpass

    # The app login often cannot create roles. A local superuser on the same
    # server can. Try the app user first, then the OS user.
    app_url = make_url(get_settings().database_url)

    async def _create_probe_role(admin) -> None:
        async with admin.begin() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role}
            )
            if exists.first() is not None:
                await conn.execute(text(f"DROP OWNED BY {role}"))
                await conn.execute(text(f"DROP ROLE {role}"))
            await conn.execute(
                text(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB "
                    f"NOCREATEROLE PASSWORD '{password}'"
                )
            )
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {app_url.database} TO {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    f"IN SCHEMA public TO {role}"
                )
            )

    created = False
    last_error: Exception | None = None
    try:
        await _create_probe_role(engine)
        created = True
    except Exception as exc:
        last_error = exc
        admin = create_async_engine(app_url.set(username=getpass.getuser(), password=""))
        try:
            await _create_probe_role(admin)
            created = True
        except Exception as exc2:
            last_error = exc2
        finally:
            await admin.dispose()
    if not created:
        pytest.skip(f"this database user cannot create a non-superuser role: {last_error}")

    probe = create_async_engine(
        make_url(get_settings().database_url).set(username=role, password=password)
    )
    try:
        async with probe.connect() as conn:
            superuser = (
                await conn.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
            assert superuser is False
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(a.id)}
            )
            names = (await conn.execute(text("SELECT name FROM customers"))).scalars().all()
            assert names == ["Ann"]
    finally:
        await probe.dispose()

        async def _drop_probe(admin) -> None:
            async with admin.begin() as conn:
                await conn.execute(text(f"DROP OWNED BY {role}"))
                await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))

        try:
            await _drop_probe(engine)
        except Exception:
            admin = create_async_engine(app_url.set(username=getpass.getuser(), password=""))
            try:
                await _drop_probe(admin)
            finally:
                await admin.dispose()
