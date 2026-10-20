"""Website chat leads: the details form, call requests, and the staff view."""

import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1 import channels as channels_api
from app.services import call_finalize
from app.services import channels as channel_svc
from app.services import widget_leads as wl
from app.services.outbound_call import OutboundCallError
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import (
    AgentStatus,
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Customer,
    PhoneNumber,
    Tenant,
)
from packages.database.models.crm import CrmLead, CrmNote, CrmStage, CrmTask
from packages.database.models.widget_lead import WidgetLead
from packages.database.session import session_scope

TEXT = {"Content-Type": "text/plain", "Origin": "https://shop.test", "User-Agent": "Mozilla/5.0 Chrome/120.0 Safari/537.36"}


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": f"{slug} salon", "tenant_slug": slug, "full_name": "Owner", "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:  # a working business has a number to call from
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        db.add(PhoneNumber(tenant_id=tenant.id, e164_number=f"+1555{uuid.uuid4().int % 10**7:07d}"))
        await db.commit()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _channel(client: AsyncClient, auth: dict, **config) -> str:
    agent = await client.post("/api/v1/ai-agents", headers=auth, json={"name": f"Bot {uuid.uuid4().hex[:6]}"})
    resp = await client.put(
        "/api/v1/channels/settings/web_chat",
        headers=auth,
        json={"enabled": True, "ai_agent_id": agent.json()["id"], "config": {"lead_form": True, "call_me": True, **config}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["public_key"]


async def _post(client: AsyncClient, key: str, path: str, body: dict) -> object:
    return await client.post(f"/api/v1/channels/webchat/{key}/{path}", content=json.dumps(body), headers=TEXT)


VISITOR = {
    "name": "Sam",
    "phone": "+919699234605",
    "email": "sam@example.com",
    "interest": "Bridal makeup on 12 Dec",
    "page_url": "https://shop.test/pricing?utm_source=google",
    "referrer": "https://www.google.com/",
    "utm": {"utm_source": "google", "utm_campaign": "diwali", "secret": "dropped"},
}


@pytest.fixture(autouse=True)
def _no_rate_limits(monkeypatch):
    async def never(bucket, *, limit, window_seconds=60):
        return False

    monkeypatch.setattr(channel_svc, "hit_rate_limit", never)


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(channels_api, "enqueue_webhook_event", lambda **kw: seen.append((kw["event"], kw["data"])))
    return seen


@pytest.fixture
def dialled(monkeypatch):
    calls: list[dict] = []

    async def fake(db, tenant_id, *, to_number, ai_agent_id=None, from_number=None):
        call = Call(tenant_id=tenant_id, direction=CallDirection.OUTBOUND, status=CallStatus.RINGING, to_number=to_number, from_number="+13613095598")
        db.add(call)
        await db.commit()
        calls.append({"to": to_number, "agent": ai_agent_id, "call_id": call.id, "from": from_number})
        return call

    monkeypatch.setattr(wl, "place_ai_outbound_call", fake)
    return calls


# --- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, country, expected",
    [
        ("+91 96992 34605", None, "+919699234605"),
        ("(+1) 415-555-0100", None, "+14155550100"),
        ("9699234605", "IN", "+919699234605"),
        ("09699234605", "IN", "+919699234605"),
        ("919699234605", "IN", "+919699234605"),
        ("0044 20 7946 0958", None, "+442079460958"),
        ("", "IN", None),
        (None, "IN", None),
        ("   ", "IN", None),
    ],
)
def test_phone_normalisation(raw, country, expected):
    assert wl.normalize_visitor_phone(raw, country) == expected


@pytest.mark.parametrize("raw, country", [("9699234605", None), ("abc", "IN"), ("12", "IN"), ("+0123456789", None), ("+1234567890123456", None), ("<script>", "IN")])
def test_bad_phone_numbers_are_refused(raw, country):
    with pytest.raises(wl.LeadError):
        wl.normalize_visitor_phone(raw, country)


def test_country_allowlist():
    assert wl.country_allowed("+919699234605", None) and wl.country_allowed("+919699234605", [])
    assert wl.country_allowed("+919699234605", ["+91", "+1"]) and wl.country_allowed("+919699234605", ["91"])
    assert not wl.country_allowed("+442079460958", ["+91", "+1"])


@pytest.mark.parametrize("text", ["Can you call me?", "please call me back", "give me a call", "I want a call", "callback please", "call karo", "मुझे कॉल करो"])
def test_call_requests_in_chat_are_recognised(text):
    assert wl.wants_call(text)


@pytest.mark.parametrize("text", ["Don't call me", "do not call me please", "what are your hours", "I called yesterday", "Hello"])
def test_other_chat_is_not_a_call_request(text):
    assert not wl.wants_call(text)


# --- channel settings -----------------------------------------------------------------------------


async def test_widget_settings_are_validated_and_exposed_to_the_widget(client: AsyncClient):
    auth = await _signup(client, "cfg-co")
    key = await _channel(client, auth, consent_text="I agree {business} can ring me.", allowed_countries=["+91"])
    cfg = (await client.get(f"/api/v1/channels/webchat/{key}/config")).json()
    assert cfg["lead_form"] is True and cfg["call_me"] is True and cfg["require_phone"] is False
    assert cfg["consent_text"] == "I agree cfg-co salon can ring me."

    for bad in (
        {"lead_form": "yes"},
        {"call_me": 1},
        {"allowed_countries": ["abc"]},
        {"allowed_countries": "+91"},
        {"max_calls_per_day": 0},
        {"max_calls_per_day": 5000},
        {"consent_text": "x" * 601},
        {"require_phone": True},  # needs the form
    ):
        resp = await client.put("/api/v1/channels/settings/web_chat", headers=auth, json={"config": bad})
        assert resp.status_code == 422, bad
    assert (await client.put("/api/v1/channels/settings/web_chat", headers=auth, json={"config": {"lead_form": True, "require_phone": True}})).status_code == 200


# --- capturing the lead --------------------------------------------------------------------------


async def test_the_details_form_stores_the_visitor_everywhere_it_is_needed(client: AsyncClient, events):
    auth = await _signup(client, "lead-co")
    key = await _channel(client, auth)
    resp = await _post(client, key, "session", VISITOR)
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_token"]

    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert (lead.name, lead.phone, lead.email) == ("Sam", "+919699234605", "sam@example.com")
        assert lead.interest == "Bridal makeup on 12 Dec" and lead.page_url.startswith("https://shop.test/pricing")
        assert lead.referrer == "https://www.google.com/" and lead.user_agent.startswith("Mozilla")
        assert lead.utm == {"utm_source": "google", "utm_campaign": "diwali"}  # only utm_ keys kept
        assert lead.call_status == "none" and lead.consent_call is False and lead.conversation_id
        customer = await db.get(Customer, lead.customer_id)
        assert customer.phone == "+919699234605" and customer.email == "sam@example.com" and customer.name == "Sam"
        crm = await db.get(CrmLead, lead.crm_lead_id)
        assert crm.source == "web_chat" and crm.tags == ["web_chat"] and crm.stage == CrmStage.NEW_LEAD
        assert crm.custom_fields["interest"] == "Bridal makeup on 12 Dec"
        notes = (await db.execute(select(CrmNote.body).where(CrmNote.lead_id == crm.id))).scalars().all()
        assert any("New lead from website chat" in n for n in notes)
    assert [e for e, _ in events] == ["lead.captured"] and events[0][1]["phone"] == "+919699234605"


@pytest.mark.parametrize(
    "override, message",
    [
        ({"name": ""}, "name"),
        ({"phone": "", "email": ""}, "phone number or an email"),
        ({"email": "nope"}, "email"),
        ({"phone": "12ab"}, "phone"),
    ],
)
async def test_the_details_form_refuses_incomplete_or_bad_details(client: AsyncClient, events, override, message):
    auth = await _signup(client, "bad-co")
    key = await _channel(client, auth)
    resp = await _post(client, key, "session", {**VISITOR, **override})
    assert resp.status_code == 422 and message in resp.json()["detail"]
    async with session_scope() as db:
        assert (await db.execute(select(WidgetLead))).first() is None
    assert events == []


async def test_a_required_phone_is_enforced_and_a_bot_trap_stores_nothing(client: AsyncClient, events):
    auth = await _signup(client, "req-co")
    key = await _channel(client, auth, require_phone=True)
    assert (await _post(client, key, "session", {**VISITOR, "phone": ""})).status_code == 422
    trapped = await _post(client, key, "session", {**VISITOR, "website": "http://spam.example"})
    assert trapped.status_code == 200 and trapped.json()["session_token"]  # looks fine to the bot
    async with session_scope() as db:
        assert (await db.execute(select(WidgetLead))).first() is None
        assert (await db.execute(select(Customer))).first() is None


async def test_a_returning_visitor_is_one_crm_lead_with_a_new_note(client: AsyncClient, events):
    auth = await _signup(client, "ret-co")
    key = await _channel(client, auth)
    await _post(client, key, "session", VISITOR)
    await _post(client, key, "session", {**VISITOR, "name": "Sam K", "interest": "Facial"})
    async with session_scope() as db:
        assert len((await db.execute(select(WidgetLead))).scalars().all()) == 2
        leads = (await db.execute(select(CrmLead))).scalars().all()
        assert len(leads) == 1
        notes = (await db.execute(select(CrmNote.body))).scalars().all()
        assert any("Came back through website chat" in n for n in notes)
        assert len((await db.execute(select(Customer))).scalars().all()) == 1


async def test_without_the_form_the_chat_works_as_before(client: AsyncClient):
    auth = await _signup(client, "plain-co")
    key = await _channel(client, auth, lead_form=False)
    resp = await _post(client, key, "session", {})
    assert resp.status_code == 200
    async with session_scope() as db:
        assert (await db.execute(select(WidgetLead))).first() is None


# --- asking for a call -----------------------------------------------------------------------------


async def _session(client, key, **over) -> str:
    return (await _post(client, key, "session", {**VISITOR, **over})).json()["session_token"]


async def test_a_call_needs_consent_then_the_ai_agent_phones_the_visitor(client: AsyncClient, events, dialled):
    auth = await _signup(client, "call-co")
    key = await _channel(client, auth, consent_text="OK to call me, {business}?")
    token = await _session(client, key)

    refused = await _post(client, key, "call-request", {"session_token": token, "consent": False})
    assert refused.status_code == 422 and "tick the box" in refused.json()["detail"]
    assert dialled == []
    assert (await _post(client, key, "call-request", {"session_token": token, "consent": "yes"})).status_code == 422  # only a real true counts

    ok = await _post(client, key, "call-request", {"session_token": token, "consent": True, "note": "After 5pm please"})
    assert ok.status_code == 200 and ok.json()["status"] == "calling" and ok.json()["call_id"]
    assert [c["to"] for c in dialled] == ["+919699234605"] and dialled[0]["agent"]
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert lead.call_status == "calling" and lead.call_attempts == 1 and lead.call_id == dialled[0]["call_id"]
        assert lead.consent_call and lead.consent_text == "OK to call me, call-co salon?" and lead.consent_at and lead.consent_ip
        assert lead.call_note == "After 5pm please"
        notes = (await db.execute(select(CrmNote.body))).scalars().all()
        assert any("AI agent is calling +919699234605" in n and "After 5pm please" in n for n in notes)

    # pressing the button again does not ring twice
    again = await _post(client, key, "call-request", {"session_token": token, "consent": True})
    assert again.json()["status"] == "calling" and len(dialled) == 1


async def test_a_visitor_can_ask_for_a_call_without_the_form(client: AsyncClient, dialled):
    auth = await _signup(client, "noform-co")
    key = await _channel(client, auth, lead_form=False)
    token = (await _post(client, key, "session", {})).json()["session_token"]
    missing = await _post(client, key, "call-request", {"session_token": token, "consent": True})
    assert missing.status_code == 422 and "name and phone" in missing.json()["detail"]
    ok = await _post(client, key, "call-request", {"session_token": token, "consent": True, "name": "Ria", "phone": "+14155550100"})
    assert ok.status_code == 200 and dialled[0]["to"] == "+14155550100"
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert lead.name == "Ria" and lead.crm_lead_id and lead.consent_call


async def test_calls_can_be_switched_off_and_limited_to_chosen_countries(client: AsyncClient, dialled):
    auth = await _signup(client, "lim-co")
    key = await _channel(client, auth, call_me=False)
    token = await _session(client, key)
    off = await _post(client, key, "call-request", {"session_token": token, "consent": True})
    assert off.status_code == 404

    key2 = await _channel(client, auth, allowed_countries=["+1"])
    token2 = await _session(client, key2)
    far = await _post(client, key2, "call-request", {"session_token": token2, "consent": True})
    assert far.status_code == 422 and "country" in far.json()["detail"]
    assert dialled == []


async def test_an_unknown_or_expired_session_cannot_request_a_call(client: AsyncClient, dialled):
    auth = await _signup(client, "exp-co")
    key = await _channel(client, auth)
    assert (await _post(client, key, "call-request", {"session_token": "nope", "consent": True})).status_code == 404
    assert (await client.post("/api/v1/channels/webchat/badkey/call-request", content="{}", headers=TEXT)).status_code == 404
    assert dialled == []


async def test_one_number_can_only_be_called_twice_a_day_through_the_widget(client: AsyncClient, dialled):
    auth = await _signup(client, "num-co")
    key = await _channel(client, auth)
    for _ in range(2):
        token = await _session(client, key)
        assert (await _post(client, key, "call-request", {"session_token": token, "consent": True})).status_code == 200
        async with session_scope() as db:  # let the 10 minute "already calling" window pass
            for lead in (await db.execute(select(WidgetLead))).scalars():
                lead.call_requested_at = datetime.now(UTC) - timedelta(minutes=30)
            await db.commit()
    third = await _post(client, key, "call-request", {"session_token": await _session(client, key), "consent": True})
    assert third.status_code == 429 and len(dialled) == 2


async def test_when_the_daily_limit_is_reached_a_person_is_asked_to_call_instead(client: AsyncClient, dialled):
    auth = await _signup(client, "cap-co")
    key = await _channel(client, auth, max_calls_per_day=1)
    first = await _post(client, key, "call-request", {"session_token": await _session(client, key, phone="+919000000001"), "consent": True})
    assert first.json()["status"] == "calling"
    second = await _post(client, key, "call-request", {"session_token": await _session(client, key, phone="+919000000002"), "consent": True})
    assert second.status_code == 200 and second.json()["status"] == "queued" and len(dialled) == 1
    async with session_scope() as db:
        task = (await db.execute(select(CrmTask))).scalar_one()
        assert "Call back Sam" in task.title and "+919000000002" in task.description and task.created_by_ai
        lead = (await db.execute(select(WidgetLead).where(WidgetLead.phone == "+919000000002"))).scalar_one()
        assert lead.consent_call and lead.call_status == "requested"


@pytest.mark.parametrize("error, status", [("+91 cannot be called: it is on this tenant's blocklist", "blocked"), ("Twilio could not reach it", "failed")])
async def test_a_call_that_cannot_be_placed_is_recorded_and_handed_to_staff(client: AsyncClient, monkeypatch, error, status):
    async def refuse(db, tenant_id, **kw):
        raise OutboundCallError(error)

    monkeypatch.setattr(wl, "place_ai_outbound_call", refuse)
    auth = await _signup(client, "fail-co")
    key = await _channel(client, auth)
    resp = await _post(client, key, "call-request", {"session_token": await _session(client, key), "consent": True})
    assert resp.status_code == 200 and resp.json()["status"] == "queued"
    assert "blocklist" not in resp.text and "Twilio" not in resp.text  # nothing internal leaks to a visitor
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert lead.call_status == status and lead.last_call_error == error and lead.consent_call
        assert (await db.execute(select(CrmTask))).scalar_one().title.startswith("Call back")


async def test_asking_for_a_call_in_the_chat_offers_the_call_card(client: AsyncClient, monkeypatch):
    async def fake_ai(*, messages, tools=None, api_key=None, **kw):
        return {"choices": [{"message": {"role": "assistant", "content": "Sure!"}}]}

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_ai, raising=False)
    auth = await _signup(client, "offer-co")
    key = await _channel(client, auth)
    token = await _session(client, key)
    yes = await _post(client, key, "message", {"session_token": token, "text": "Can you call me please?"})
    no = await _post(client, key, "message", {"session_token": token, "text": "What are your hours?"})
    never = await _post(client, key, "message", {"session_token": token, "text": "Don't call me"})
    assert yes.json()["call_offer"] is True and no.json()["call_offer"] is False and never.json()["call_offer"] is False

    key2 = await _channel(client, auth, call_me=False)
    token2 = await _session(client, key2)
    off = await _post(client, key2, "message", {"session_token": token2, "text": "call me"})
    assert off.json()["call_offer"] is False


# --- after the call ---------------------------------------------------------------------------------


async def _lead_with_call(client, slug, dialled):
    auth = await _signup(client, slug)
    key = await _channel(client, auth)
    await _post(client, key, "call-request", {"session_token": await _session(client, key), "consent": True})
    return auth, dialled[0]["call_id"]


async def test_a_finished_call_moves_the_lead_forward(client: AsyncClient, dialled):
    _, call_id = await _lead_with_call(client, "done-co", dialled)
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        call.status = CallStatus.IN_PROGRESS
        call.started_at = datetime.now(UTC) - timedelta(seconds=135)
        await db.commit()
        await call_finalize.finalize_call(db, call, status=CallStatus.COMPLETED)
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert lead.call_status == "completed"
        crm = await db.get(CrmLead, lead.crm_lead_id)
        assert crm.stage == CrmStage.CONTACTED and crm.source_call_id == call_id
        notes = (await db.execute(select(CrmNote.body))).scalars().all()
        assert any("spoke with them for 2m" in n for n in notes)
        assert (await db.execute(select(CrmTask))).first() is None


@pytest.mark.parametrize("final, expected", [(CallStatus.NO_ANSWER, "no_answer"), (CallStatus.FAILED, "failed")])
async def test_an_unanswered_call_asks_staff_to_follow_up(client: AsyncClient, dialled, final, expected):
    _, call_id = await _lead_with_call(client, f"miss-{expected.replace('_', '-')}", dialled)
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        await call_finalize.finalize_call(db, call, status=final)
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert lead.call_status == expected
        assert (await db.execute(select(CrmTask))).scalar_one().title.startswith("Call back")
        assert (await db.get(CrmLead, lead.crm_lead_id)).stage == CrmStage.NEW_LEAD


async def test_calls_that_did_not_come_from_the_widget_are_untouched(client: AsyncClient):
    auth = await _signup(client, "other-co")
    async with session_scope() as db:
        from packages.database.models import Tenant

        tenant = (await db.execute(select(Tenant))).scalar_one()
        call = Call(tenant_id=tenant.id, direction=CallDirection.INBOUND, status=CallStatus.IN_PROGRESS)
        db.add(call)
        await db.commit()
        assert await call_finalize.finalize_call(db, call, status=CallStatus.COMPLETED)
    assert auth


# --- the staff view ------------------------------------------------------------------------------------


async def test_staff_can_search_filter_inspect_and_summarise_leads(client: AsyncClient, dialled):
    auth = await _signup(client, "staff-co")
    key = await _channel(client, auth)
    await _post(client, key, "session", {**VISITOR, "name": "Asha", "phone": "+919000000001", "email": "asha@example.com"})
    t2 = (await _post(client, key, "session", {**VISITOR, "name": "Bo_b%", "phone": "+919000000002", "email": "bob@example.com"})).json()["session_token"]
    await _post(client, key, "call-request", {"session_token": t2, "consent": True})

    everyone = (await client.get("/api/v1/widget-leads", headers=auth)).json()
    assert {lead["name"] for lead in everyone} == {"Asha", "Bo_b%"}
    assert [lead["name"] for lead in (await client.get("/api/v1/widget-leads?call_status=calling", headers=auth)).json()] == ["Bo_b%"]
    assert [lead["name"] for lead in (await client.get("/api/v1/widget-leads?q=asha", headers=auth)).json()] == ["Asha"]
    assert [lead["name"] for lead in (await client.get("/api/v1/widget-leads?q=000002", headers=auth)).json()] == ["Bo_b%"]
    assert (await client.get("/api/v1/widget-leads?q=%25", headers=auth)).json()[0]["name"] == "Bo_b%"  # % is literal, not a wildcard
    assert (await client.get("/api/v1/widget-leads?q=A_ha", headers=auth)).json() == []  # nor is _ a one-letter wildcard
    assert (await client.get("/api/v1/widget-leads?call_status=weird", headers=auth)).status_code == 422

    detail = (await client.get(f"/api/v1/widget-leads/{everyone[0]['id']}", headers=auth)).json()
    assert detail["utm"] == {"utm_source": "google", "utm_campaign": "diwali"} and detail["referrer"] and detail["ip_address"]
    summary = (await client.get("/api/v1/widget-leads/summary", headers=auth)).json()
    assert summary == {"days": 30, "leads": 2, "with_phone": 2, "call_requests": 1, "calls_completed": 0, "needs_follow_up": 0}


async def test_staff_can_only_call_leads_who_agreed(client: AsyncClient, dialled):
    auth = await _signup(client, "stcall-co")
    key = await _channel(client, auth)
    await _post(client, key, "session", VISITOR)
    lead_id = (await client.get("/api/v1/widget-leads", headers=auth)).json()[0]["id"]
    no = await client.post(f"/api/v1/widget-leads/{lead_id}/call", headers=auth)
    assert no.status_code == 409 and "agreed" in no.json()["detail"] and dialled == []

    await _post(client, key, "call-request", {"session_token": await _session(client, key), "consent": True})
    async with session_scope() as db:
        for lead in (await db.execute(select(WidgetLead))).scalars():
            lead.call_requested_at = datetime.now(UTC) - timedelta(hours=1)
            lead.call_status = "no_answer"
        await db.commit()
    agreed = (await client.get("/api/v1/widget-leads?call_status=no_answer", headers=auth)).json()[0]["id"]
    ok = await client.post(f"/api/v1/widget-leads/{agreed}/call", headers=auth)
    assert ok.status_code == 200 and ok.json()["call_status"] == "calling" and ok.json()["call_attempts"] == 2
    busy = await client.post(f"/api/v1/widget-leads/{agreed}/call", headers=auth)
    assert busy.status_code == 409 and "already in progress" in busy.json()["detail"]


async def test_export_includes_consent_is_csv_safe_audited_and_tenant_scoped(client: AsyncClient, dialled):
    auth = await _signup(client, "exp-led")
    other = await _signup(client, "exp-led2")
    key = await _channel(client, auth)
    t = await _session(client, key, name="=cmd|' /C calc'!A0")
    await _post(client, key, "call-request", {"session_token": t, "consent": True})

    resp = await client.get("/api/v1/widget-leads/export", headers=auth)
    assert resp.status_code == 200 and 'filename="chat-leads.csv"' in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    header, body = rows[0], rows[1:]
    assert "call_consent" in header and "consent_text" in header and len(body) == 1
    record = dict(zip(header, body[0], strict=True))
    assert record["call_consent"] == "yes" and record["consent_text"] and record["name"].startswith("'=")
    audited = (await client.get("/api/v1/audit-logs", headers=auth, params={"action": "widget_leads"})).json()
    assert [a["action"] for a in audited] == ["widget_leads.exported"]
    theirs = list(csv.reader(io.StringIO((await client.get("/api/v1/widget-leads/export", headers=other)).text)))
    assert len(theirs) == 1  # header only


async def test_other_businesses_and_anonymous_users_see_nothing(client: AsyncClient, dialled):
    a = await _signup(client, "iso-a")
    b = await _signup(client, "iso-b")
    key = await _channel(client, a)
    await _post(client, key, "session", VISITOR)
    lead_id = (await client.get("/api/v1/widget-leads", headers=a)).json()[0]["id"]
    assert (await client.get("/api/v1/widget-leads", headers=b)).json() == []
    assert (await client.get(f"/api/v1/widget-leads/{lead_id}", headers=b)).status_code == 404
    assert (await client.post(f"/api/v1/widget-leads/{lead_id}/call", headers=b)).status_code == 404
    assert (await client.get("/api/v1/widget-leads")).status_code == 401
    assert (await client.get("/api/v1/widget-leads/export")).status_code == 401
    assert uuid.UUID(lead_id)


# --- which agent and which number make the call -------------------------------------------------------


def test_the_country_of_a_number_is_read_from_its_calling_code():
    assert wl.calling_code("+919876543210") == "91"
    assert wl.calling_code("+14155550100") == "1"
    assert wl.calling_code("+971501234567") == "971"
    assert wl.calling_code("+442079460958") == "44"
    assert wl.calling_code("+999123") is None and wl.calling_code("") is None


async def _business(slug="num-biz", agents=(("Receptionist", "active"),), numbers=()):
    """A business with the given agents and numbers (number, assigned agent name or None), oldest first."""
    from packages.database.models import Tenant as T

    async with session_scope() as db:
        tenant = T(name=slug, slug=slug)
        db.add(tenant)
        await db.flush()
        by_name = {}
        for name, status in agents:
            agent = AIAgent(tenant_id=tenant.id, name=name, status=AgentStatus(status))
            db.add(agent)
            await db.flush()
            by_name[name] = agent.id
        for number, owner in numbers:
            db.add(PhoneNumber(tenant_id=tenant.id, e164_number=number, ai_agent_id=by_name.get(owner)))
            await db.flush()
        await db.commit()
        return tenant.id, by_name


async def _choose(tenant_id, channel_agent=None, config=None, to="+919876543210"):
    async with session_scope() as db:
        return await wl.choose_call_setup(db, tenant_id, channel_agent_id=channel_agent, config=config or {}, to_number=to)


async def test_the_chats_agent_and_its_only_number_are_used():
    tenant, ag = await _business("one-num", numbers=[("+14155550101", "Receptionist")])
    assert await _choose(tenant, ag["Receptionist"]) == (ag["Receptionist"], "+14155550101")


async def test_a_business_with_two_numbers_and_no_agent_on_the_chat_still_gets_a_call():
    # What happened in production: the chat had no agent and the business owns two numbers.
    tenant, ag = await _business("two-num", numbers=[("+13613095598", "Receptionist"), ("+12602541794", "Receptionist")])
    agent_id, number = await _choose(tenant, None, to="+919699234605")
    assert agent_id == ag["Receptionist"] and number == "+13613095598"  # the oldest, no number is from India


async def test_a_number_from_the_visitors_own_country_is_preferred():
    tenant, ag = await _business("local-num", numbers=[("+13613095598", "Receptionist"), ("+912240001234", "Receptionist"), ("+919800000000", "Receptionist")])
    assert (await _choose(tenant, ag["Receptionist"], to="+919699234605"))[1] == "+912240001234"  # oldest Indian one
    assert (await _choose(tenant, ag["Receptionist"], to="+14155550100"))[1] == "+13613095598"
    assert (await _choose(tenant, ag["Receptionist"], to="+442079460958"))[1] == "+13613095598"  # no match: oldest


async def test_the_number_the_owner_picked_wins_but_only_if_it_is_theirs():
    tenant, ag = await _business("pick-num", numbers=[("+13613095598", "Receptionist"), ("+912240001234", "Receptionist")])
    assert (await _choose(tenant, ag["Receptionist"], {"call_from_number": "+13613095598"}, to="+919699234605"))[1] == "+13613095598"
    # a number that isn't theirs is ignored, never dialled from
    assert (await _choose(tenant, ag["Receptionist"], {"call_from_number": "+19999999999"}, to="+919699234605"))[1] == "+912240001234"


async def test_numbers_not_assigned_to_the_agent_are_used_when_it_has_none():
    tenant, ag = await _business("unassigned", agents=(("A", "active"), ("B", "active")), numbers=[("+14155550102", None), ("+14155550103", "B")])
    assert await _choose(tenant, ag["A"]) == (ag["A"], "+14155550102")  # A has none: any number, oldest first
    assert await _choose(tenant, ag["B"]) == (ag["B"], "+14155550103")  # B's own number first


async def test_with_no_agent_chosen_the_one_that_owns_a_number_is_used():
    tenant, ag = await _business("owner-agent", agents=(("First", "active"), ("Second", "active")), numbers=[("+14155550104", "Second")])
    assert (await _choose(tenant, None))[0] == ag["Second"]


async def test_with_nothing_else_the_oldest_active_agent_is_used_and_drafts_are_skipped():
    tenant, ag = await _business("oldest", agents=(("Draft", "draft"), ("Live", "active")), numbers=[("+14155550105", None)])
    assert (await _choose(tenant, None))[0] == ag["Live"]


async def test_an_agent_the_owner_attached_is_used_even_if_still_a_draft():
    tenant, ag = await _business("draft-ok", agents=(("Draft", "draft"), ("Live", "active")), numbers=[("+14155550106", None)])
    assert (await _choose(tenant, ag["Draft"]))[0] == ag["Draft"]


async def test_another_businesss_agent_is_never_used():
    mine, mine_ag = await _business("mine", numbers=[("+14155550107", None)])
    _, theirs_ag = await _business("theirs", agents=(("Other", "active"),))
    assert (await _choose(mine, theirs_ag["Other"]))[0] == mine_ag["Receptionist"]


async def test_clear_reasons_when_there_is_nothing_to_call_with():
    no_numbers, _ = await _business("no-numbers")
    with pytest.raises(OutboundCallError, match="no phone number"):
        await _choose(no_numbers)
    no_agents, _ = await _business("no-agents", agents=(("Draft", "draft"),), numbers=[("+14155550108", None)])
    with pytest.raises(OutboundCallError, match="no active AI agent"):
        await _choose(no_agents)


async def test_the_visitor_gets_the_call_from_the_right_number_even_when_the_chat_has_no_agent(client: AsyncClient, dialled):
    auth = await _signup(client, "prod-case")
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "prod-case"))).scalar_one()
        db.add(PhoneNumber(tenant_id=tenant.id, e164_number="+912240009999"))  # a second number: the production situation
        db.add(AIAgent(tenant_id=tenant.id, name="Receptionist", status=AgentStatus.ACTIVE))
        await db.commit()
    resp = await client.put("/api/v1/channels/settings/web_chat", headers=auth, json={"enabled": True, "config": {"lead_form": True, "call_me": True}})
    key = resp.json()["public_key"]
    assert resp.json()["ai_agent_id"] is None
    token = (await _post(client, key, "session", {**VISITOR, "phone": "+919699234605"})).json()["session_token"]
    ok = await _post(client, key, "call-request", {"session_token": token, "consent": True})
    assert ok.status_code == 200 and ok.json()["status"] == "calling", ok.text
    assert dialled[0]["from"] == "+912240009999" and dialled[0]["agent"]  # an Indian number for an Indian visitor


async def test_a_business_with_no_number_hands_the_visitor_to_staff_with_a_clear_reason(client: AsyncClient, dialled):
    auth = await _signup(client, "nonum-case")
    async with session_scope() as db:
        for number in (await db.execute(select(PhoneNumber))).scalars():
            await db.delete(number)
        await db.commit()
    key = await _channel(client, auth)
    token = await _session(client, key)
    resp = await _post(client, key, "call-request", {"session_token": token, "consent": True})
    assert resp.status_code == 200 and resp.json()["status"] == "queued" and "phone number" not in resp.text
    assert dialled == []
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        assert lead.call_status == "failed" and "no phone number" in lead.last_call_error
        assert (await db.execute(select(CrmTask))).scalar_one().title.startswith("Call back")


async def test_staff_calling_again_uses_the_same_choice(client: AsyncClient, dialled):
    auth = await _signup(client, "staff-num")
    key = await _channel(client, auth)
    await _post(client, key, "call-request", {"session_token": await _session(client, key), "consent": True})
    async with session_scope() as db:
        for lead in (await db.execute(select(WidgetLead))).scalars():
            lead.call_requested_at = datetime.now(UTC) - timedelta(hours=1)
            lead.call_status = "no_answer"
        await db.commit()
    lead_id = (await client.get("/api/v1/widget-leads", headers=auth)).json()[0]["id"]
    again = await client.post(f"/api/v1/widget-leads/{lead_id}/call", headers=auth)
    assert again.status_code == 200 and len(dialled) == 2 and dialled[1]["from"] == dialled[0]["from"]
