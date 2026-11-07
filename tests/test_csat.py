"""CSAT surveys: scheduling rules, sending (WhatsApp/SMS), reply capture, reporting."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.services import csat
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    ChannelSettings,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
    Ticket,
)
from packages.database.models.csat import CsatSettings, CsatSurvey
from packages.database.session import session_scope


async def _tenant(enabled=True, **settings):
    async with session_scope() as db:
        tenant = Tenant(name="Csat Co", slug="csat-co")
        db.add(tenant)
        await db.flush()
        customer = Customer(tenant_id=tenant.id, name="Sam", phone="+15551230000")
        db.add(customer)
        db.add(CsatSettings(tenant_id=tenant.id, enabled=enabled, **settings))
        await db.commit()
        return tenant.id, customer.id


@pytest.fixture
def sent(monkeypatch):
    log: list[tuple[str, str]] = []

    async def wa(db, tenant_id, customer, body):
        log.append(("whatsapp", body))

    async def sms(db, tenant_id, customer, body, *, meter=True):
        log.append(("sms", body))

    monkeypatch.setattr(csat, "_send_whatsapp", wa)
    monkeypatch.setattr(csat, "_send_sms", sms)
    return log


async def _schedule(tenant_id, customer_id, source_type="call", source_id=None, **kw):
    import uuid

    async with session_scope() as db:
        survey = await csat.schedule_survey(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
            source_type=source_type,
            source_id=source_id or uuid.uuid4(),
            **kw,
        )
        await db.commit()
        return survey.id if survey else None


def test_parse_rating():
    assert csat.parse_rating("4") == (4, None)
    assert csat.parse_rating(" 5 - great, thanks ") == (5, "great, thanks")
    assert csat.parse_rating("3!") == (3, None)
    assert csat.parse_rating("1 terrible") == (1, "terrible")
    for bad in ("0", "6", "40", "hello 4", "", "four"):
        assert csat.parse_rating(bad) is None, bad


async def test_scheduling_rules():
    tenant_id, customer_id = await _tenant(min_call_seconds=15, cooldown_hours=24, delay_minutes=5)
    assert await _schedule(tenant_id, customer_id, call_duration_seconds=5) is None  # too short
    first = await _schedule(tenant_id, customer_id, call_duration_seconds=60)
    assert first is not None
    assert await _schedule(tenant_id, customer_id, "ticket") is None  # cooldown
    async with session_scope() as db:
        survey = await db.get(CsatSurvey, first)
        assert survey.status == "pending"
        assert timedelta(minutes=4) < survey.send_at - datetime.now(UTC) <= timedelta(minutes=5)


async def test_disabled_missing_phone_and_source_flags():
    tenant_id, customer_id = await _tenant(enabled=False)
    assert await _schedule(tenant_id, customer_id, call_duration_seconds=60) is None
    async with session_scope() as db:
        settings = await csat.get_settings_row(db, tenant_id)
        settings.enabled, settings.after_tickets = True, False
        customer = await db.get(Customer, customer_id)
        customer.phone = None
        await db.commit()
    assert await _schedule(tenant_id, customer_id, call_duration_seconds=60) is None  # no phone
    async with session_scope() as db:
        (await db.get(Customer, customer_id)).phone = "+15551230000"
        await db.commit()
    assert await _schedule(tenant_id, customer_id, "ticket") is None  # tickets off
    assert await _schedule(tenant_id, customer_id, call_duration_seconds=60) is not None


async def test_same_source_is_only_surveyed_once():
    import uuid

    tenant_id, customer_id = await _tenant(cooldown_hours=0)
    source = uuid.uuid4()
    assert await _schedule(tenant_id, customer_id, "ticket", source) is not None
    assert await _schedule(tenant_id, customer_id, "ticket", source) is None


async def test_due_surveys_are_sent_and_old_ones_expire(sent):
    tenant_id, customer_id = await _tenant(channel="sms", cooldown_hours=0, delay_minutes=0)
    survey_id = await _schedule(tenant_id, customer_id, call_duration_seconds=60)
    async with session_scope() as db:
        result = await csat.send_due_surveys(db)
    assert result == {"sent": 1, "failed": 0, "expired": 0} and sent[0][0] == "sms"
    async with session_scope() as db:
        survey = await db.get(CsatSurvey, survey_id)
        assert survey.status == "sent" and survey.channel == "sms"
        result = await csat.send_due_surveys(db, now=datetime.now(UTC) + timedelta(hours=49))
    assert result["expired"] == 1


async def test_send_failure_is_recorded(monkeypatch):
    tenant_id, customer_id = await _tenant(channel="sms", delay_minutes=0)

    async def boom(db, tenant_id, customer, body, *, meter=True):
        raise csat.SurveySendError("The SMS channel is not enabled")

    monkeypatch.setattr(csat, "_send_sms", boom)
    survey_id = await _schedule(tenant_id, customer_id, call_duration_seconds=60)
    async with session_scope() as db:
        assert (await csat.send_due_surveys(db))["failed"] == 1
        survey = await db.get(CsatSurvey, survey_id)
        assert survey.status == "failed" and "SMS channel" in survey.error


async def test_auto_channel_uses_whatsapp_only_inside_the_window(sent, monkeypatch):
    tenant_id, customer_id = await _tenant(channel="auto", cooldown_hours=0, delay_minutes=0)
    async with session_scope() as db:
        conv = Conversation(tenant_id=tenant_id, customer_id=customer_id, channel=ConversationChannel.WHATSAPP)
        db.add(conv)
        await db.flush()
        db.add(
            Message(
                tenant_id=tenant_id,
                conversation_id=conv.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.CUSTOMER,
                text="hi",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await db.commit()
    await _schedule(tenant_id, customer_id, call_duration_seconds=60)
    async with session_scope() as db:
        await csat.send_due_surveys(db)
    assert [c for c, _ in sent] == ["whatsapp"]

    async with session_scope() as db:  # window now closed -> SMS
        msg = (await db.execute(select(Message))).scalars().first()
        msg.created_at = datetime.now(UTC) - timedelta(hours=30)
        await db.commit()
    await _schedule(tenant_id, customer_id, "ticket")
    async with session_scope() as db:
        await csat.send_due_surveys(db)
    assert [c for c, _ in sent] == ["whatsapp", "sms"]


async def _answer(tenant_id, customer_id, text, channel=ConversationChannel.SMS):
    async with session_scope() as db:
        thanks = await csat.capture_reply(
            db, tenant_id=tenant_id, customer_id=customer_id, channel=channel, text=text
        )
        await db.commit()
        return thanks


async def _sent_survey(sent, **settings):
    tenant_id, customer_id = await _tenant(channel="sms", cooldown_hours=0, delay_minutes=0, **settings)
    await _schedule(tenant_id, customer_id, call_duration_seconds=60)
    async with session_scope() as db:
        await csat.send_due_surveys(db)
    return tenant_id, customer_id


async def test_reply_is_captured_once_and_only_on_the_right_channel(sent):
    tenant_id, customer_id = await _sent_survey(sent)
    assert await _answer(tenant_id, customer_id, "hello there") is None
    assert await _answer(tenant_id, customer_id, "5", ConversationChannel.WHATSAPP) is None  # wrong channel
    assert await _answer(tenant_id, customer_id, "5 - superb") == "Thank you for your feedback!"
    assert await _answer(tenant_id, customer_id, "5") is None  # already answered
    async with session_scope() as db:
        survey = (await db.execute(select(CsatSurvey))).scalar_one()
        assert (survey.status, survey.rating, survey.comment) == ("answered", 5, "superb")


async def test_expired_survey_cannot_be_answered(sent):
    tenant_id, customer_id = await _sent_survey(sent)
    async with session_scope() as db:
        survey = (await db.execute(select(CsatSurvey))).scalar_one()
        survey.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    assert await _answer(tenant_id, customer_id, "4") is None


async def test_low_rating_opens_a_high_priority_ticket(sent):
    tenant_id, customer_id = await _sent_survey(sent)
    await _answer(tenant_id, customer_id, "1 waited forever")
    async with session_scope() as db:
        ticket = (await db.execute(select(Ticket))).scalar_one()
        assert ticket.priority.value == "high" and "Low CSAT (1/5)" in ticket.subject
        assert ticket.customer_id == customer_id


async def test_threshold_zero_disables_tickets(sent):
    tenant_id, customer_id = await _sent_survey(sent, low_score_ticket_threshold=0)
    await _answer(tenant_id, customer_id, "1")
    async with session_scope() as db:
        assert (await db.execute(select(Ticket))).first() is None


async def test_summary_math(sent):
    tenant_id, customer_id = await _tenant(cooldown_hours=0, delay_minutes=0, channel="sms")
    async with session_scope() as db:
        now = datetime.now(UTC)
        import uuid

        for rating in (5, 4, 4, 2, None):
            db.add(
                CsatSurvey(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    source_type="call" if rating != 2 else "ticket",
                    source_id=uuid.uuid4(),
                    channel="sms",
                    status="answered" if rating else "sent",
                    rating=rating,
                    send_at=now,
                    sent_at=now,
                )
            )
        await db.commit()
        result = await csat.summary(db, tenant_id)
    assert result["surveys_sent"] == 5 and result["responses"] == 4
    assert result["response_rate"] == 0.8 and result["average_rating"] == 3.75
    assert result["csat_percent"] == 75.0
    assert result["distribution"] == {"1": 0, "2": 1, "3": 0, "4": 2, "5": 1}
    assert result["by_source"]["ticket"] == {"average_rating": 2.0, "responses": 1}


async def test_finishing_a_call_and_resolving_a_ticket_schedule_surveys(client: AsyncClient):
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "Flow Co", "tenant_slug": "flow-co", "full_name": "O", "email": "o@flow-co.com", "password": "supersecret1"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    assert (await client.patch("/api/v1/csat/settings", headers=headers, json={"enabled": True, "cooldown_hours": 0})).status_code == 200
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant))).scalar_one()
        customer = Customer(tenant_id=tenant.id, name="Pat", phone="+15557770000")
        db.add(customer)
        await db.flush()
        call = Call(
            tenant_id=tenant.id,
            customer_id=customer.id,
            direction=CallDirection.INBOUND,
            status=CallStatus.IN_PROGRESS,
            started_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        db.add(call)
        await db.commit()
        customer_id, call_id = customer.id, call.id
    from app.services.call_finalize import finalize_call

    async with session_scope() as db:
        await finalize_call(db, await db.get(Call, call_id), status=CallStatus.COMPLETED)

    ticket = await client.post("/api/v1/tickets", headers=headers, json={"customer_id": str(customer_id), "subject": "Help"})
    resolved = await client.patch(f"/api/v1/tickets/{ticket.json()['id']}", headers=headers, json={"status": "resolved"})
    assert resolved.status_code == 200
    surveys = (await client.get("/api/v1/csat/surveys", headers=headers)).json()
    # the cooldown is 0, so both the call and the ticket got a survey
    assert sorted(s["source_type"] for s in surveys) == ["call", "ticket"]
    summary = await client.get("/api/v1/csat/summary", headers=headers)
    assert summary.status_code == 200 and summary.json()["responses"] == 0


async def test_settings_api_validates_and_is_tenant_scoped(client: AsyncClient):
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "Set Co", "tenant_slug": "set-co", "full_name": "O", "email": "o@set-co.com", "password": "supersecret1"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    default = (await client.get("/api/v1/csat/settings", headers=headers)).json()
    assert default["enabled"] is False and default["channel"] == "auto"
    ok = await client.patch("/api/v1/csat/settings", headers=headers, json={"channel": "sms", "question": "Rate us 1-5?"})
    assert ok.json()["channel"] == "sms" and ok.json()["question"] == "Rate us 1-5?"
    assert (await client.patch("/api/v1/csat/settings", headers=headers, json={"channel": "fax"})).status_code == 422
    assert (await client.patch("/api/v1/csat/settings", headers=headers, json={"delay_minutes": -1})).status_code == 422
    assert (await client.get("/api/v1/csat/settings")).status_code == 401


async def test_inbound_sms_rating_is_stored_and_not_sent_to_the_ai(client: AsyncClient, monkeypatch, sent):
    from app.services import channels as channel_service
    from packages.database.models import PhoneNumber

    tenant_id, customer_id = await _tenant(channel="sms", cooldown_hours=0, delay_minutes=0)
    async with session_scope() as db:
        db.add(PhoneNumber(tenant_id=tenant_id, e164_number="+15559990000"))
        db.add(ChannelSettings(tenant_id=tenant_id, channel="sms", enabled=True, config={}))
        await db.commit()
    await _schedule(tenant_id, customer_id, call_duration_seconds=60)
    async with session_scope() as db:
        await csat.send_due_surveys(db)

    replies: list[str] = []

    async def fake_deliver(db, conversation, text):
        replies.append(text)

    monkeypatch.setattr(channel_service, "deliver_reply", fake_deliver)

    async def no_ai(**kwargs):
        raise AssertionError("the AI must not see a CSAT rating")

    monkeypatch.setattr(channel_service, "run_turn", no_ai)
    resp = await client.post(
        "/api/v1/channels/sms/webhook",
        data={"To": "+15559990000", "From": "+15551230000", "Body": "4 nice", "MessageSid": "SM1"},
    )
    assert resp.status_code == 200
    assert replies == ["Thank you for your feedback!"]
    async with session_scope() as db:
        survey = (await db.execute(select(CsatSurvey))).scalar_one()
        assert survey.rating == 4 and survey.comment == "nice"
        texts = [m.text for m in (await db.execute(select(Message).order_by(Message.created_at))).scalars()]
        assert texts == ["4 nice", "Thank you for your feedback!"]
