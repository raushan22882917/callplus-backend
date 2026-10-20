"""Spend budget, alerts, safety caps and the cost breakdown."""

from datetime import UTC, datetime, timedelta

import pytest
from app.services import cost_controls as cc
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Customer,
    Tenant,
)
from packages.database.models.cost_controls import CostAlert, CostControls
from packages.database.models.platform import PricingPlan
from packages.database.session import session_scope


async def _tenant(slug="cost-co", *, rate=10.0, **controls):
    """A tenant with a plan charging `rate` per AI minute and the given controls."""
    async with session_scope() as db:
        tenant = Tenant(name="Cost Co", slug=slug)
        db.add(tenant)
        await db.flush()
        db.add(Customer(tenant_id=tenant.id, name="Sam", phone="+15551230000"))
        db.add(PricingPlan(tenant_id=tenant.id, name="P", is_active=True, rates={"ai_minutes": rate}))
        if controls:
            db.add(
                CostControls(
                    tenant_id=tenant.id,
                    alert_thresholds=controls.pop("alert_thresholds", [50, 80, 100]),
                    alert_emails=controls.pop("alert_emails", []),
                    **controls,
                )
            )
        await db.commit()
        return tenant.id


async def _agent(tenant_id, name="Receptionist"):
    async with session_scope() as db:
        agent = AIAgent(tenant_id=tenant_id, name=name)
        db.add(agent)
        await db.commit()
        return agent.id


async def _call(
    tenant_id,
    *,
    minutes=0,
    direction=CallDirection.OUTBOUND,
    status=CallStatus.COMPLETED,
    agent_id=None,
    age=timedelta(0),
):
    async with session_scope() as db:
        at = datetime.now(UTC) - age
        db.add(
            Call(
                tenant_id=tenant_id,
                direction=direction,
                status=status,
                ai_agent_id=agent_id,
                duration_seconds=int(minutes * 60),
                started_at=at,
                created_at=at,
                to_number="+15550001111",
                from_number="+15550002222",
            )
        )
        await db.commit()


@pytest.fixture
def notified(monkeypatch):
    log: dict[str, list] = {"emails": [], "webhooks": []}

    async def fake_email(*, to_email, subject, html_body, headers=None):
        log["emails"].append((to_email, subject))

    monkeypatch.setattr(cc, "send_email", fake_email)
    monkeypatch.setattr(
        cc, "enqueue_webhook_event", lambda **kw: log["webhooks"].append((kw["event"], kw["data"]))
    )
    return log


def test_crossed_level_and_forecast():
    assert cc.crossed_level(49.9, [50, 80, 100]) == 0
    assert cc.crossed_level(50, [50, 80, 100]) == 50
    assert cc.crossed_level(95, [50, 80, 100]) == 80
    assert cc.crossed_level(250, [50, 80, 100]) == 100
    assert cc.crossed_level(10, []) == 0
    # day 11 at noon of a 30-day month: 10.5 days elapsed
    assert cc.forecast_month_end(105, now=datetime(2026, 11, 11, 12, tzinfo=UTC)) == 300
    # too early to extrapolate
    assert cc.forecast_month_end(5, now=datetime(2026, 11, 1, 2, tzinfo=UTC)) == 5


async def test_no_controls_means_no_limits():
    tenant_id = await _tenant()
    async with session_scope() as db:
        await cc.guard_outbound(db, tenant_id)  # does not raise
        assert (await cc.evaluate_alerts(db, tenant_id)) is None


async def test_daily_cap_counts_only_todays_outbound_calls():
    tenant_id = await _tenant(daily_outbound_call_cap=2)
    await _call(tenant_id, minutes=1)
    await _call(tenant_id, minutes=1, direction=CallDirection.INBOUND)  # never counts
    await _call(tenant_id, minutes=1, age=timedelta(days=2))  # an earlier day
    async with session_scope() as db:
        await cc.guard_outbound(db, tenant_id)  # 1 of 2 used
    await _call(tenant_id, minutes=1)
    async with session_scope() as db:
        with pytest.raises(cc.SpendGuardError) as err:
            await cc.guard_outbound(db, tenant_id)
        assert err.value.code == "daily_cap" and err.value.http_status == 429
        with pytest.raises(cc.SpendGuardError):
            await cc.guard_outbound(db, tenant_id)
        # a block is logged once per hour, not once per refused call
        blocks = (await db.execute(select(CostAlert).where(CostAlert.kind == "blocked"))).scalars().all()
        assert len(blocks) == 1


async def test_concurrency_cap_ignores_stuck_rows():
    tenant_id = await _tenant(max_concurrent_outbound=2)
    await _call(tenant_id, status=CallStatus.RINGING)
    await _call(tenant_id, status=CallStatus.IN_PROGRESS)
    await _call(tenant_id, status=CallStatus.RINGING, age=timedelta(hours=3))  # stuck, not live
    async with session_scope() as db:
        with pytest.raises(cc.SpendGuardError) as err:
            await cc.guard_outbound(db, tenant_id)
        assert err.value.code == "concurrency_cap"
    async with session_scope() as db:
        row = (await db.execute(select(CostControls))).scalar_one()
        row.max_concurrent_outbound = 3
        await db.commit()
    async with session_scope() as db:
        await cc.guard_outbound(db, tenant_id)  # room for one more


async def test_budget_pause_blocks_outbound_only_when_enabled():
    agent_tenant = await _tenant(monthly_budget=50, pause_outbound_at_budget=False)
    agent = await _agent(agent_tenant)
    await _call(agent_tenant, minutes=10, agent_id=agent)  # 10 min x 10 = 100 > 50
    async with session_scope() as db:
        await cc.guard_outbound(db, agent_tenant)  # not paused: only alerts
        row = (await db.execute(select(CostControls))).scalar_one()
        row.pause_outbound_at_budget = True
        await db.commit()
    async with session_scope() as db:
        with pytest.raises(cc.SpendGuardError) as err:
            await cc.guard_outbound(db, agent_tenant)
        assert err.value.code == "budget_exhausted" and err.value.http_status == 402
        assert "50" in err.value.message


async def test_alerts_fire_once_per_level_and_reset_with_a_new_budget(notified):
    tenant_id = await _tenant(monthly_budget=200, alert_emails=["boss@example.com"])
    agent = await _agent(tenant_id)
    await _call(tenant_id, minutes=10, agent_id=agent)  # 100 of 200 = 50%
    async with session_scope() as db:
        first = await cc.evaluate_alerts(db, tenant_id)
        assert first is not None and first.level == 50 and first.kind == "budget"
        assert await cc.evaluate_alerts(db, tenant_id) is None  # same level: silent
    assert notified["emails"] == [("boss@example.com", "Budget alert: 50% of your monthly budget used")]
    assert notified["webhooks"][0][0] == "budget.alert"
    assert notified["webhooks"][0][1]["level"] == 50

    await _call(tenant_id, minutes=7, agent_id=agent)  # 170 of 200 = 85%
    async with session_scope() as db:
        second = await cc.evaluate_alerts(db, tenant_id)
        assert second is not None and second.level == 80
    await _call(tenant_id, minutes=4, agent_id=agent)  # 210 of 200
    async with session_scope() as db:
        third = await cc.evaluate_alerts(db, tenant_id)
        assert third is not None and third.level == 100
        assert await cc.evaluate_alerts(db, tenant_id) is None
    assert len(notified["emails"]) == 3


async def test_a_failing_email_never_breaks_alerting(monkeypatch, notified):
    async def boom(**kw):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(cc, "send_email", boom)
    tenant_id = await _tenant(monthly_budget=50, alert_emails=["a@example.com"])
    agent = await _agent(tenant_id)
    await _call(tenant_id, minutes=10, agent_id=agent)
    async with session_scope() as db:
        alert = await cc.evaluate_alerts(db, tenant_id)
        assert alert is not None and alert.level == 100


async def test_status_snapshot_reports_spend_caps_and_pause():
    tenant_id = await _tenant(
        monthly_budget=100, daily_outbound_call_cap=5, pause_outbound_at_budget=True
    )
    agent = await _agent(tenant_id)
    await _call(tenant_id, minutes=5, agent_id=agent)  # 50
    await _call(tenant_id, minutes=1, agent_id=agent, status=CallStatus.RINGING)
    async with session_scope() as db:
        snap = await cc.status_snapshot(db, tenant_id)
    assert snap["spend"] == 60.0 and snap["budget"] == 100.0
    assert snap["percent_used"] == 60.0 and snap["level"] == 50
    assert snap["outbound_today"] == 2 and snap["daily_outbound_call_cap"] == 5
    assert snap["outbound_in_flight"] == 1
    assert snap["outbound_paused"] is False


async def test_cost_breakdown_by_agent_day_and_top_calls():
    tenant_id = await _tenant()
    busy, quiet = await _agent(tenant_id, "Busy"), await _agent(tenant_id, "Quiet")
    await _call(tenant_id, minutes=8, agent_id=busy)
    await _call(tenant_id, minutes=2, agent_id=quiet)
    await _call(tenant_id, minutes=5, direction=CallDirection.INBOUND)  # human, rate 0 under this plan
    async with session_scope() as db:
        data = await cc.cost_breakdown(db, tenant_id)
    assert data["total"] == 100.0  # 10 AI minutes x 10
    assert [a["minutes"] for a in data["by_agent"]] == [8.0, 2.0]
    assert [a["cost"] for a in data["by_agent"]] == [80.0, 20.0]
    assert sum(d["cost"] for d in data["daily"]) == 100.0
    assert data["top_calls"][0]["minutes"] == 8.0 and data["top_calls"][0]["cost"] == 80.0
    assert data["avg_cost_per_call"] == pytest.approx(33.33, abs=0.01)


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug,
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"o@{slug}.com",
            "password": "supersecret1",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_settings_api_defaults_validation_and_clearing(client: AsyncClient):
    h = await _signup(client, "api-co")
    assert (await client.get("/api/v1/cost-controls")).status_code == 401
    base = (await client.get("/api/v1/cost-controls", headers=h)).json()
    assert base["monthly_budget"] is None and base["alert_thresholds"] == [50, 80, 100]
    assert base["pause_outbound_at_budget"] is False

    ok = await client.patch(
        "/api/v1/cost-controls",
        headers=h,
        json={
            "monthly_budget": 5000,
            "alert_thresholds": [90, 50, 50],
            "alert_emails": [" Boss@Example.com ", "boss@example.com"],
            "daily_outbound_call_cap": 200,
            "max_concurrent_outbound": 5,
            "pause_outbound_at_budget": True,
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["alert_thresholds"] == [50, 90] and body["alert_emails"] == ["boss@example.com"]

    for bad in (
        {"monthly_budget": -1},
        {"alert_thresholds": [0]},
        {"alert_thresholds": [1, 2, 3, 4, 5, 6, 7]},
        {"alert_emails": ["not-an-email"]},
        {"daily_outbound_call_cap": 0},
        {"pause_outbound_at_budget": None},
    ):
        resp = await client.patch("/api/v1/cost-controls", headers=h, json=bad)
        assert resp.status_code == 422, bad

    cleared = await client.patch(
        "/api/v1/cost-controls", headers=h, json={"daily_outbound_call_cap": None}
    )
    assert cleared.json()["daily_outbound_call_cap"] is None
    assert cleared.json()["monthly_budget"] == 5000  # untouched fields stay

    status = (await client.get("/api/v1/cost-controls/status", headers=h)).json()
    assert status["budget"] == 5000 and status["spend"] == 0 and status["outbound_paused"] is False
    assert (await client.get("/api/v1/cost-controls/breakdown", headers=h)).status_code == 200
    assert (await client.get("/api/v1/cost-controls/breakdown?month=bad", headers=h)).status_code == 400
    assert (await client.get("/api/v1/cost-controls/alerts", headers=h)).json() == []


async def test_other_tenants_cannot_see_or_change_each_others_controls(client: AsyncClient):
    a = await _signup(client, "tenant-a")
    b = await _signup(client, "tenant-b")
    await client.patch("/api/v1/cost-controls", headers=a, json={"monthly_budget": 123})
    assert (await client.get("/api/v1/cost-controls", headers=b)).json()["monthly_budget"] is None


async def test_public_api_refuses_outbound_calls_over_the_daily_cap(client: AsyncClient):
    h = await _signup(client, "cap-co")
    await client.patch("/api/v1/cost-controls", headers=h, json={"daily_outbound_call_cap": 1})
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "cap-co"))).scalar_one()
        tenant_id = tenant.id
    agent_id = await _agent(tenant_id)
    await _call(tenant_id)  # the one allowed call is already used
    resp = await client.post(
        "/api/v1/calls",
        headers=h,
        json={
            "direction": "outbound",
            "to_number": "+919876543210",
            "from_number": "+14155550100",
            "ai_agent_id": str(agent_id),
        },
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["code"] == "daily_cap"


async def test_campaign_pauses_instead_of_failing_contacts_when_a_cap_is_hit(monkeypatch):
    from app.worker import campaign_tasks

    from packages.database.models import (
        Campaign,
        CampaignChannel,
        CampaignContact,
        CampaignContactStatus,
        CampaignStatus,
        PhoneNumber,
    )

    tenant_id = await _tenant("camp-co", daily_outbound_call_cap=1)
    agent_id = await _agent(tenant_id)
    async with session_scope() as db:
        db.add(PhoneNumber(tenant_id=tenant_id, e164_number="+14155550100", ai_agent_id=agent_id))
        campaign = Campaign(
            tenant_id=tenant_id,
            name="Outreach",
            channel=CampaignChannel.VOICE,
            status=CampaignStatus.RUNNING,
            ai_agent_id=agent_id,
        )
        db.add(campaign)
        await db.flush()
        for n in range(3):
            customer = Customer(tenant_id=tenant_id, name=f"c{n}", phone=f"+91900000000{n}")
            db.add(customer)
            await db.flush()
            db.add(
                CampaignContact(
                    tenant_id=tenant_id, campaign_id=campaign.id, customer_id=customer.id
                )
            )
        await db.commit()
        campaign_id = str(campaign.id)

    dialled: list[str] = []
    monkeypatch.setattr(
        campaign_tasks,
        "place_outbound_call",
        lambda **kw: dialled.append(kw["to"]) or f"CA{len(dialled)}",
    )

    async def no_amd(db, agent_id):
        return None

    monkeypatch.setattr(campaign_tasks, "amd_callback_for_agent", no_amd)
    await campaign_tasks._run_campaign(campaign_id)

    assert len(dialled) == 1  # the daily cap of 1 allowed exactly one call
    async with session_scope() as db:
        campaign = (await db.execute(select(Campaign))).scalar_one()
        statuses = sorted(c.status.value for c in (await db.execute(select(CampaignContact))).scalars())
    assert campaign.status == CampaignStatus.PAUSED
    assert statuses == [
        CampaignContactStatus.PENDING.value,
        CampaignContactStatus.PENDING.value,
        CampaignContactStatus.SENT.value,
    ]
