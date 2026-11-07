"""Post-call email and SMS to the customer, and the summary email to the team."""

from datetime import UTC, datetime

import pytest
from app.services import call_finalize, csat
from app.services import post_call as pc
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import Call, CallDirection, CallStatus, Customer, Tenant
from packages.database.models.cost_controls import CostControls
from packages.database.models.platform import PricingPlan, UsageEvent
from packages.database.models.post_call import CallNotification, PostCallSettings
from packages.database.session import session_scope


async def _setup(*, enabled=True, phone="+919699234605", email="sam@example.com", **settings):
    async with session_scope() as db:
        tenant = Tenant(name="Apna Salon", slug="apna-salon")
        db.add(tenant)
        await db.flush()
        customer = Customer(tenant_id=tenant.id, name="Sam", phone=phone, email=email)
        db.add(customer)
        db.add(PostCallSettings(tenant_id=tenant.id, enabled=enabled, staff_emails=settings.pop("staff_emails", []), **settings))
        await db.flush()
        call = Call(
            tenant_id=tenant.id,
            customer_id=customer.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.COMPLETED,
            duration_seconds=95,
            summary="You booked a haircut for today at 3 pm.",
            intent="book a salon haircut",
            sentiment="positive",
            to_number=phone,
            from_number="+13613095598",
            started_at=datetime.now(UTC),
        )
        db.add(call)
        await db.commit()
        return tenant.id, call.id


@pytest.fixture
def outbox(monkeypatch):
    sent: dict[str, list] = {"sms": [], "email": []}

    async def sms(db, tenant_id, customer, body, *, meter=True):
        sent["sms"].append((customer.phone, body))

    async def email(*, to_email, subject, html_body, headers=None):
        sent["email"].append((to_email, subject, html_body))

    monkeypatch.setattr(csat, "_send_sms", sms)
    monkeypatch.setattr(pc, "send_email", email)
    return sent


async def _run(call_id):
    async with session_scope() as db:
        return await pc.notify_after_call(db, call_id)


async def _log():
    async with session_scope() as db:
        rows = (await db.execute(select(CallNotification))).scalars().all()
        return sorted((r.channel, r.audience, r.status) for r in rows)


def test_templates_fill_only_known_tokens_and_fit_an_sms():
    out = pc.fill("Hi {name}, thanks for calling {business}. {summary} {other}", business="Apna Salon", name="Sam", summary="Booked.")
    assert out == "Hi Sam, thanks for calling Apna Salon. Booked. {other}"
    assert pc.fill("Thanks {summary}!", business="B", name="", summary="") == "Thanks !"
    long = pc.sms_text("{summary}", business="B", name="", summary="word " * 200)
    assert len(long) <= pc.SMS_MAX_CHARS and long.endswith("…")
    assert pc.sms_segments("x" * 160) == 1 and pc.sms_segments("x" * 161) == 2 and pc.sms_segments("") == 1


async def test_nothing_is_sent_until_the_business_turns_it_on(outbox):
    _, call_id = await _setup(enabled=False)
    assert await _run(call_id) == {"skipped": "disabled"}
    assert outbox == {"sms": [], "email": []}


async def test_customer_gets_an_sms_and_an_email_with_the_summary(outbox):
    _, call_id = await _setup()
    result = await _run(call_id)
    assert result == {"sent": 2}
    assert outbox["sms"] == [("+919699234605", "Thanks for calling Apna Salon. You booked a haircut for today at 3 pm.")]
    to, subject, body = outbox["email"][0]
    assert to == "sam@example.com" and subject == "Thanks for calling Apna Salon"
    assert "Hi Sam," in body and "You booked a haircut" in body
    assert await _log() == [("email", "customer", "sent"), ("sms", "customer", "sent")]


async def test_running_it_twice_never_double_sends(outbox):
    _, call_id = await _setup(staff_email=True, staff_emails=["boss@example.com"])
    await _run(call_id)
    await _run(call_id)
    assert len(outbox["sms"]) == 1 and len(outbox["email"]) == 2  # customer + one staff address
    assert len(await _log()) == 3


async def test_the_team_gets_a_summary_with_the_details(outbox):
    _, call_id = await _setup(customer_sms=False, customer_email=False, staff_email=True, staff_emails=["boss@example.com", "ops@example.com"])
    await _run(call_id)
    assert outbox["sms"] == []
    assert sorted(to for to, _, _ in outbox["email"]) == ["boss@example.com", "ops@example.com"]
    body = outbox["email"][0][2]
    assert "Sam" in body and "book a salon haircut" in body and "positive" in body and "You booked a haircut" in body


async def test_customer_content_is_escaped_in_emails(outbox):
    async with session_scope() as db:
        tenant = Tenant(name="<b>Evil</b> & Co", slug="evil")
        db.add(tenant)
        await db.flush()
        customer = Customer(tenant_id=tenant.id, name="<script>x</script>", phone="+911", email="a@example.com")
        db.add(customer)
        db.add(PostCallSettings(tenant_id=tenant.id, enabled=True, customer_sms=False, staff_emails=[]))
        await db.flush()
        call = Call(tenant_id=tenant.id, customer_id=customer.id, direction=CallDirection.INBOUND, status=CallStatus.COMPLETED, duration_seconds=60, summary="<img src=x onerror=1>")
        db.add(call)
        await db.commit()
        call_id = call.id
    await _run(call_id)
    body = outbox["email"][0][2]
    assert "<script>" not in body and "<img" not in body and "&lt;script&gt;" in body


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"duration_seconds": 4}, "too_short"),
        ({"outcome": "voicemail"}, "voicemail"),
        ({"answered_by": "machine_end_beep"}, "voicemail"),
        ({"status": CallStatus.NO_ANSWER}, "not_completed"),
        ({"status": CallStatus.FAILED}, "not_completed"),
    ],
)
async def test_short_voicemail_and_unfinished_calls_send_nothing(outbox, override, reason):
    _, call_id = await _setup()
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        for k, v in override.items():
            setattr(call, k, v)
        await db.commit()
    assert await _run(call_id) == {"skipped": reason}
    assert outbox == {"sms": [], "email": []}


async def test_a_customer_without_a_phone_or_email_just_gets_what_they_can(outbox):
    _, call_id = await _setup(phone=None, email="only@example.com")
    assert await _run(call_id) == {"sent": 1}
    assert outbox["sms"] == [] and len(outbox["email"]) == 1


async def test_summary_can_be_left_out(outbox):
    _, call_id = await _setup(include_summary=False)
    await _run(call_id)
    assert outbox["sms"][0][1] == "Thanks for calling Apna Salon."
    assert "haircut" not in outbox["email"][0][2]


async def test_sms_not_set_up_fails_that_message_but_the_email_still_goes(monkeypatch):
    emails = []

    async def email(*, to_email, subject, html_body, headers=None):
        emails.append(to_email)

    monkeypatch.setattr(pc, "send_email", email)  # csat._send_sms is real: no SMS channel exists
    _, call_id = await _setup()
    result = await _run(call_id)
    assert result == {"failed": 1, "sent": 1} and emails == ["sam@example.com"]
    async with session_scope() as db:
        sms = (await db.execute(select(CallNotification).where(CallNotification.channel == "sms"))).scalar_one()
        assert sms.status == "failed" and "SMS channel" in sms.error


async def test_a_broken_email_server_is_recorded_not_raised(monkeypatch):
    async def sms(db, tenant_id, customer, body, *, meter=True):
        return None

    async def boom(**kw):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(csat, "_send_sms", sms)
    monkeypatch.setattr(pc, "send_email", boom)
    _, call_id = await _setup()
    assert await _run(call_id) == {"failed": 1, "sent": 1}


async def test_sms_counts_toward_usage_and_the_budget(outbox):
    tenant_id, call_id = await _setup(sms_message="x" * 200)
    async with session_scope() as db:
        db.add(PricingPlan(tenant_id=tenant_id, name="P", is_active=True, rates={"sms": 2.0}))
        await db.commit()
    await _run(call_id)
    async with session_scope() as db:
        event = (
            await db.execute(select(UsageEvent).where(UsageEvent.metric == "sms"))
        ).scalar_one()
        assert event.metric == "sms" and float(event.quantity) == 2  # 200 chars = 2 segments
        assert event.reference_id == call_id


async def test_paid_sms_is_held_back_when_the_budget_is_used_up_but_email_is_not(outbox):
    tenant_id, call_id = await _setup()
    async with session_scope() as db:
        db.add(PricingPlan(tenant_id=tenant_id, name="P", is_active=True, rates={"sms": 10.0}))
        db.add(UsageEvent(tenant_id=tenant_id, metric="sms", quantity=50, occurred_at=datetime.now(UTC)))
        db.add(CostControls(tenant_id=tenant_id, monthly_budget=100, pause_outbound_at_budget=True, alert_thresholds=[100], alert_emails=[]))
        await db.commit()
    assert await _run(call_id) == {"skipped": 1, "sent": 1}
    assert outbox["sms"] == [] and len(outbox["email"]) == 1


async def test_finishing_a_call_queues_the_messages_after_a_delay(monkeypatch):
    queued = []
    from app.worker import tasks

    monkeypatch.setattr(tasks.notify_after_call_task, "apply_async", lambda **kw: queued.append(kw))
    _, call_id = await _setup()
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        call.status = CallStatus.IN_PROGRESS
        await db.commit()
        assert await call_finalize.finalize_call(db, call, status=CallStatus.COMPLETED)
    assert queued == [{"args": [str(call_id)], "countdown": call_finalize.POST_CALL_DELAY_SECONDS}]

    queued.clear()
    async with session_scope() as db:
        other = Call(tenant_id=(await db.get(Call, call_id)).tenant_id, direction=CallDirection.OUTBOUND, status=CallStatus.RINGING)
        db.add(other)
        await db.commit()
        await call_finalize.finalize_call(db, other, status=CallStatus.NO_ANSWER)
    assert queued == []  # an unanswered call sends nothing


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug, "tenant_slug": slug, "full_name": "Owner", "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_settings_api_defaults_validation_and_isolation(client: AsyncClient):
    a = await _signup(client, "pc-a")
    b = await _signup(client, "pc-b")
    assert (await client.get("/api/v1/post-call/settings")).status_code == 401
    base = (await client.get("/api/v1/post-call/settings", headers=a)).json()
    assert base["enabled"] is False and base["customer_sms"] is True and base["min_call_seconds"] == 10
    assert "{business}" in base["sms_message"]

    ok = await client.patch(
        "/api/v1/post-call/settings",
        headers=a,
        json={"enabled": True, "staff_email": True, "staff_emails": [" Boss@Example.com ", "boss@example.com"], "min_call_seconds": 30},
    )
    assert ok.status_code == 200 and ok.json()["staff_emails"] == ["boss@example.com"] and ok.json()["enabled"] is True
    for bad in ({"staff_emails": ["nope"]}, {"min_call_seconds": -1}, {"sms_message": "x" * 321}, {"sms_message": "hi"}, {"staff_emails": [f"a{n}@example.com" for n in range(6)]}):
        assert (await client.patch("/api/v1/post-call/settings", headers=a, json=bad)).status_code == 422, bad
    assert (await client.get("/api/v1/post-call/settings", headers=b)).json()["enabled"] is False
    assert (await client.get("/api/v1/post-call/log?status=weird", headers=a)).status_code == 422
    assert (await client.get("/api/v1/post-call/log", headers=a)).json() == []
