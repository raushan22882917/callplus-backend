import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1.telephony_plus import router as telephony_plus_router
from app.main import app
from app.services import dialer, ivr
from app.services import telephony_plus as svc
from app.worker import campaign_tasks
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
    ConsentRecord,
    Customer,
    PhoneNumber,
    Ticket,
)
from packages.database.models.telephony_plus import (  # noqa: F401 — registers tables
    CampaignContactState,
    CampaignDialerSettings,
    CampaignOutcome,
    DndEntry,
)

if not any(getattr(r, "path", "").startswith("/api/v1/telephony_plus") for r in app.routes):
    app.include_router(telephony_plus_router, prefix="/api/v1")

BASE = "/api/v1/telephony_plus"


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


# --- dialer.next_batch ---------------------------------------------------------------------


def test_preview_never_autodials() -> None:
    assert dialer.next_batch(
        mode="preview", available_agents=5, in_flight=0, answer_rate=0.5,
        max_concurrent=10, remaining_contacts=100,
    ) == 0  # fmt: skip


def test_progressive_is_one_call_per_free_agent_minus_in_flight() -> None:
    kwargs = dict(mode="progressive", answer_rate=0.3, max_concurrent=50, remaining_contacts=100)
    assert dialer.next_batch(available_agents=4, in_flight=0, **kwargs) == 4
    assert dialer.next_batch(available_agents=4, in_flight=3, **kwargs) == 1
    assert dialer.next_batch(available_agents=4, in_flight=9, **kwargs) == 0
    assert dialer.next_batch(available_agents=0, in_flight=0, **kwargs) == 0


def test_power_uses_pacing_ratio_and_respects_caps() -> None:
    kwargs = dict(mode="power", answer_rate=0.3, pacing_ratio=2.5, remaining_contacts=100)
    assert dialer.next_batch(available_agents=4, in_flight=0, max_concurrent=50, **kwargs) == 10
    assert dialer.next_batch(available_agents=4, in_flight=4, max_concurrent=50, **kwargs) == 6
    assert dialer.next_batch(available_agents=4, in_flight=0, max_concurrent=7, **kwargs) == 7
    assert dialer.next_batch(
        available_agents=4, in_flight=0, max_concurrent=50, **{**kwargs, "remaining_contacts": 3}
    ) == 3  # fmt: skip
    assert dialer.next_batch(
        available_agents=4, in_flight=0, max_concurrent=50, daily_remaining=2, **kwargs
    ) == 2  # fmt: skip


def test_predictive_ratio_follows_answer_rate_and_backs_off_on_abandons() -> None:
    kwargs = dict(
        mode="predictive", pacing_ratio=1.0, max_concurrent=100, remaining_contacts=100,
        abandon_rate_cap=0.03, available_agents=10, in_flight=0,
    )  # fmt: skip
    assert dialer.next_batch(answer_rate=0.5, abandon_rate=0.0, **kwargs) == 20  # ratio 2
    assert dialer.next_batch(answer_rate=0.25, abandon_rate=0.0, **kwargs) == 40  # ratio 4 (max)
    assert dialer.next_batch(answer_rate=0.01, abandon_rate=0.0, **kwargs) == 40  # clamped
    # near the cap: ratio 2 -> 1.5
    assert dialer.next_batch(answer_rate=0.5, abandon_rate=0.026, **kwargs) == 15
    # at/over the cap: fall back to 1:1
    assert dialer.next_batch(answer_rate=0.5, abandon_rate=0.03, **kwargs) == 10


def test_rates_from_outcomes() -> None:
    assert dialer.answer_and_abandon_rates(attempts=10, answered=4, abandoned=1) == (0.4, 0.25)
    assert dialer.answer_and_abandon_rates(attempts=0, answered=0, abandoned=0) == (0.3, 0.0)


def test_timezone_from_country_code_state_and_explicit() -> None:
    assert dialer.timezone_for_contact("+919876543210") == "Asia/Kolkata"
    assert dialer.timezone_for_contact("+442071234567") == "Europe/London"
    assert dialer.timezone_for_contact("+14155550123", extra={"state": "ca"}) == "America/Los_Angeles"
    assert dialer.timezone_for_contact("+14155550123") == "America/New_York"
    assert dialer.timezone_for_contact("+919876543210", extra={"timezone": "Asia/Dubai"}) == "Asia/Dubai"
    assert dialer.timezone_for_contact("+919876543210", extra={"timezone": "Nope/Zone"}) == "Asia/Kolkata"
    assert dialer.timezone_for_contact("12345", default="UTC") == "UTC"


def test_quiet_hours_and_call_window_per_timezone() -> None:
    # 2026-03-10 15:00 UTC == 20:30 IST (inside 9-21) == 08:00 in New York... (EDT 11:00)
    now = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    kw = dict(window_start="09:00", window_end="21:00", quiet_start="21:00", quiet_end="09:00")
    assert dialer.can_call_now(now, tz_name="Asia/Kolkata", **kw)
    # 16:00 UTC == 21:30 IST: past 9pm -> blocked by window and quiet hours
    assert not dialer.can_call_now(now + timedelta(hours=1), tz_name="Asia/Kolkata", **kw)
    # 03:00 UTC == 08:30 IST: before 9am
    assert not dialer.can_call_now(datetime(2026, 3, 10, 3, 0, tzinfo=UTC), tz_name="Asia/Kolkata", **kw)
    assert dialer.can_call_now(datetime(2026, 3, 10, 3, 30, tzinfo=UTC), tz_name="Asia/Kolkata", **kw)
    # same instant is fine in New York
    assert dialer.can_call_now(now + timedelta(hours=1), tz_name="America/New_York", **kw)
    # overnight window semantics
    assert dialer.in_window(dialer.parse_hhmm("23:30"), "21:00", "09:00")
    assert dialer.in_window(dialer.parse_hhmm("08:59"), "21:00", "09:00")
    assert not dialer.in_window(dialer.parse_hhmm("12:00"), "21:00", "09:00")


def test_retry_delay_repeats_last_entry() -> None:
    schedule = [10, 60]
    assert [dialer.retry_delay_minutes(schedule, n) for n in (1, 2, 3, 9)] == [10, 60, 60, 60]
    assert dialer.retry_delay_minutes([], 1) == 60


# --- TwiML builders ----------------------------------------------------------------------------


def test_menu_twiml_is_a_gather_with_redirect_fallback() -> None:
    root = _root(
        ivr.build_menu_twiml(
            greeting="Press 1 for sales", language="en-IN", digit_url="https://x/digit?a=1&b=2"
        )
    )
    gather = root.find("Gather")
    assert gather is not None
    assert gather.get("numDigits") == "1" and gather.get("input") == "dtmf"
    assert gather.get("action") == "https://x/digit?a=1&b=2"
    assert gather.find("Say").text == "Press 1 for sales"
    assert root.find("Redirect") is not None


def test_enqueue_wait_and_agent_twiml() -> None:
    enqueue = _root(ivr.build_enqueue_twiml(queue_name="q1", wait_url="https://x/wait", result_url="https://x/res"))
    node = enqueue.find("Enqueue")
    assert node.text == "q1" and node.get("waitUrl") == "https://x/wait"
    assert node.get("action") == "https://x/res"

    wait = _root(
        ivr.build_queue_wait_twiml(
            position=2, hold_message="Hold on", music_url=None, queue_time_seconds=10, max_wait_seconds=60
        )
    )
    assert "number 2" in wait.find("Say").text
    assert "on other calls" in wait.find("Say").text
    assert wait.find("Play") is not None and wait.find("Leave") is None

    expired = _root(
        ivr.build_queue_wait_twiml(
            position=2, hold_message="Hold on", music_url=None, queue_time_seconds=60, max_wait_seconds=60
        )
    )
    assert expired.find("Leave") is not None

    agent = _root(ivr.build_queue_agent_twiml("q1"))
    assert agent.find("Dial/Queue").text == "q1"


def test_ring_group_simultaneous_and_sequential() -> None:
    members = ["+911111111111", "+922222222222", "+933333333333"]
    simul = _root(ivr.build_ring_group_twiml(members, strategy="simultaneous", timeout=15, next_url="https://x/n"))
    assert [n.text for n in simul.findall("Dial/Number")] == members
    assert simul.find("Dial").get("timeout") == "15"

    seq = _root(
        ivr.build_ring_group_twiml(members, strategy="sequential", timeout=15, next_url="https://x/n?c=1", index=1)
    )
    assert [n.text for n in seq.findall("Dial/Number")] == [members[1]]
    assert seq.find("Dial").get("action") == "https://x/n?c=1&idx=2"

    assert _root(ivr.build_ring_group_twiml([], strategy="simultaneous", timeout=5)).find("Hangup") is not None


def test_masked_bridge_presents_tenant_caller_id() -> None:
    root = _root(ivr.build_masked_bridge_twiml(customer_number="+919999999999", caller_id="+918888888888"))
    dial = root.find("Dial")
    assert dial.get("callerId") == "+918888888888"
    assert dial.find("Number").text == "+919999999999"


def test_park_voicemail_and_forward_twiml() -> None:
    park = _root(ivr.build_park_twiml(conference_name="park-1", wait_url="https://x/w"))
    conf = park.find("Dial/Conference")
    assert conf.text == "park-1" and conf.get("waitUrl") == "https://x/w"
    assert conf.get("startConferenceOnEnter") == "false"

    vm = _root(ivr.build_voicemail_twiml(message="Leave a note", language="en-IN", recording_callback_url="https://x/r"))
    assert vm.find("Record").get("recordingStatusCallback") == "https://x/r"

    fwd = _root(ivr.build_forward_twiml("+911234567890", caller_id="+910000000000"))
    assert fwd.find("Dial").get("callerId") == "+910000000000"


# --- IVR option validation / digit resolution / business hours -------------------------------------


def test_digit_resolution_and_validation() -> None:
    options = {"1": {"action": "queue", "target_id": "q"}, "2": {"action": "forward", "number": "+911"}}
    assert ivr.resolve_digit(options, "1")["action"] == "queue"
    assert ivr.resolve_digit(options, "9") is None
    assert ivr.resolve_digit(options, "") is None
    assert ivr.resolve_digit(options, None) is None
    assert ivr.validate_options(options) == []
    problems = ivr.validate_options({"12": {"action": "hangup"}, "3": {"action": "queue"}, "4": {"action": "bogus"}})
    assert len(problems) == 3


def test_business_hours_and_holidays() -> None:
    windows = [{"weekday": 1, "start": "09:00", "end": "17:00"}]  # Tuesday
    tue_noon_ist = datetime(2026, 3, 10, 6, 30, tzinfo=UTC)  # 12:00 IST Tuesday
    assert ivr.is_open_now(tue_noon_ist, tz_name="Asia/Kolkata", windows=windows)
    assert not ivr.is_open_now(tue_noon_ist + timedelta(hours=7), tz_name="Asia/Kolkata", windows=windows)
    assert not ivr.is_open_now(
        tue_noon_ist, tz_name="Asia/Kolkata", windows=windows, holiday_days={datetime(2026, 3, 10).date()}
    )
    assert ivr.is_open_now(tue_noon_ist, tz_name="Asia/Kolkata", windows=[])


# --- API / webhook flows ---------------------------------------------------------------------------


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _tenant_id(db: AsyncSession, slug: str) -> uuid.UUID:
    from packages.database.models import Tenant

    return (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()


async def test_inbound_ivr_flow_digit_routing(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _signup(client, "ivr-co")
    tenant_id = await _tenant_id(db_session, "ivr-co")
    db_session.add(PhoneNumber(tenant_id=tenant_id, e164_number="+15550001111"))  # no AI agent
    await db_session.commit()

    agent = (await client.post("/api/v1/ai-agents", headers=headers, json={"name": "Bot"})).json()
    queue = (
        await client.post(
            f"{BASE}/queues", headers=headers,
            json={"name": "Sales", "members": ["+919000000001"], "max_wait_seconds": 60},
        )
    ).json()  # fmt: skip
    group = (
        await client.post(
            f"{BASE}/ring-groups", headers=headers,
            json={"name": "Support", "members": ["+919000000002", "+919000000003"], "strategy": "sequential"},
        )
    ).json()  # fmt: skip

    bad = await client.post(
        f"{BASE}/ivr-menus", headers=headers,
        json={"name": "Main", "greeting_text": "Hi", "options": {"1": {"action": "queue", "target_id": str(uuid.uuid4())}}},
    )  # fmt: skip
    assert bad.status_code == 422

    resp = await client.post(
        f"{BASE}/ivr-menus", headers=headers,
        json={
            "name": "Main", "number": "+15550001111", "greeting_text": "Press 1 for sales",
            "options": {
                "1": {"action": "queue", "target_id": queue["id"]},
                "2": {"action": "ring_group", "target_id": group["id"]},
                "3": {"action": "ai_agent", "target_id": agent["id"]},
                "4": {"action": "forward", "number": "+919555555555"},
                "5": {"action": "voicemail", "message": "Leave a message"},
                "9": {"action": "hangup", "message": "Bye"},
            },
        },
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    menu_id = resp.json()["id"]

    voice = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15550001111", "From": "+15552223333", "CallSid": "CAivr1"},
    )
    assert voice.status_code == 200
    root = _root(voice.text)
    gather = root.find("Gather")
    assert gather is not None and f"/ivr/{menu_id}/digit" in gather.get("action")
    call = (await db_session.execute(select(Call).where(Call.provider_call_sid == "CAivr1"))).scalar_one()
    digit_url = f"{BASE}/ivr/{menu_id}/digit?call_id={call.id}&attempt=0"

    async def press(digits: str, attempt: int = 0) -> ET.Element:
        r = await client.post(
            f"{BASE}/ivr/{menu_id}/digit?call_id={call.id}&attempt={attempt}",
            data={"Digits": digits, "CallSid": "CAivr1"},
        )
        assert r.status_code == 200, r.text
        return _root(r.text)

    assert digit_url  # sanity

    enq = (await press("1")).find("Enqueue")
    assert enq is not None and f"/queues/{queue['id']}/wait" in enq.get("waitUrl")

    ring = await press("2")
    assert [n.text for n in ring.findall("Dial/Number")] == ["+919000000002"]  # sequential: first member

    stream = await press("3")
    assert stream.find("Connect/Stream") is not None
    await db_session.refresh(call)
    assert str(call.ai_agent_id) == agent["id"]

    fwd = await press("4")
    assert fwd.find("Dial/Number").text == "+919555555555"
    assert fwd.find("Dial").get("callerId") == "+15550001111"  # tenant number, not the agent's

    assert (await press("5")).find("Record") is not None
    assert (await press("9")).find("Hangup") is not None

    again = await press("7", attempt=0)  # invalid -> re-prompt with attempt 1
    assert again.find("Gather") is not None and "attempt=1" in again.find("Gather").get("action")
    assert "not a valid choice" in again.find("Gather/Say").text
    gone = await press("7", attempt=2)  # retries exhausted
    assert gone.find("Hangup") is not None and gone.find("Gather") is None


async def test_queue_wait_result_overflow_and_agent_webhooks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _signup(client, "queue-co")
    tenant_id = await _tenant_id(db_session, "queue-co")
    queue = (
        await client.post(
            f"{BASE}/queues", headers=headers,
            json={
                "name": "Billing", "max_wait_seconds": 30, "hold_message": "Thanks for waiting",
                "overflow_action": {"action": "voicemail", "message": "Leave it"},
            },
        )
    ).json()  # fmt: skip
    call = Call(tenant_id=tenant_id, direction=CallDirection.INBOUND, to_number="+15550001111")
    db_session.add(call)
    await db_session.commit()

    wait = _root(
        (await client.post(f"{BASE}/queues/{queue['id']}/wait", data={"QueuePosition": "3", "QueueTime": "5"})).text
    )
    assert "number 3" in wait.find("Say").text
    left = _root(
        (await client.post(f"{BASE}/queues/{queue['id']}/wait", data={"QueuePosition": "3", "QueueTime": "31"})).text
    )
    assert left.find("Leave") is not None

    overflow = _root(
        (
            await client.post(
                f"{BASE}/queues/{queue['id']}/result?call_id={call.id}", data={"QueueResult": "queue-timeout"}
            )
        ).text
    )
    assert overflow.find("Record") is not None
    bridged = _root(
        (
            await client.post(
                f"{BASE}/queues/{queue['id']}/result?call_id={call.id}", data={"QueueResult": "bridged"}
            )
        ).text
    )
    assert bridged.find("Record") is None

    agent_leg = _root((await client.post(f"{BASE}/queues/{queue['id']}/agent")).text)
    assert agent_leg.find("Dial/Queue") is not None

    # another tenant can't see/modify it
    other = await _signup(client, "queue-other")
    assert (await client.get(f"{BASE}/queues/{queue['id']}", headers=other)).status_code == 404


async def test_forwarding_rule_overrides_ivr(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _signup(client, "fwd-co")
    tenant_id = await _tenant_id(db_session, "fwd-co")
    db_session.add(PhoneNumber(tenant_id=tenant_id, e164_number="+15550002222"))
    await db_session.commit()
    resp = await client.post(
        f"{BASE}/forwarding-rules", headers=headers,
        json={"number": "+15550002222", "destination": "+919777777777"},
    )  # fmt: skip
    assert resp.status_code == 201
    voice = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15550002222", "From": "+15552223333", "CallSid": "CAfwd1"},
    )
    assert _root(voice.text).find("Dial/Number").text == "+919777777777"


async def test_number_routing_to_queue(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _signup(client, "route-co")
    tenant_id = await _tenant_id(db_session, "route-co")
    db_session.add(PhoneNumber(tenant_id=tenant_id, e164_number="+15550003333"))
    await db_session.commit()
    queue = (await client.post(f"{BASE}/queues", headers=headers, json={"name": "Front"})).json()
    r = await client.post(
        f"{BASE}/number-routing", headers=headers,
        json={"e164_number": "+15550003333", "department": "Front desk", "target_type": "queue", "target_id": queue["id"]},
    )  # fmt: skip
    assert r.status_code == 201
    dup = await client.post(
        f"{BASE}/number-routing", headers=headers,
        json={"e164_number": "+15550003333", "target_type": "queue", "target_id": queue["id"]},
    )  # fmt: skip
    assert dup.status_code == 409
    voice = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15550003333", "From": "+15552223333", "CallSid": "CAroute1"},
    )
    said = _root(voice.text).find("Say")
    assert said is not None and "busy" in said.text


async def test_unrouted_number_without_agent_still_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, "plain-co")
    tenant_id = await _tenant_id(db_session, "plain-co")
    db_session.add(PhoneNumber(tenant_id=tenant_id, e164_number="+15550004444"))
    await db_session.commit()
    voice = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15550004444", "From": "+15552223333", "CallSid": "CAplain"},
    )
    assert "<Reject" in voice.text


# --- DND ------------------------------------------------------------------------------------------


async def test_dnd_registry_and_outbound_guard(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _signup(client, "dnd-co")
    tenant_id = await _tenant_id(db_session, "dnd-co")

    assert (await client.post(f"{BASE}/dnd", headers=headers, json={"e164_number": "+919811111111"})).status_code == 201
    assert (await client.post(f"{BASE}/dnd", headers=headers, json={"e164_number": "+919811111111"})).status_code == 409
    assert len((await client.get(f"{BASE}/dnd", headers=headers)).json()) == 1

    check = await client.post(
        f"{BASE}/dnd/check", headers=headers,
        json={"e164_number": "+919811111111", "enforce_quiet_hours": False},
    )  # fmt: skip
    assert check.json() == {"allowed": False, "reason": "dnd"}

    decision = await svc.check_outbound_allowed(db_session, tenant_id, "+919811111111")
    assert (decision.allowed, decision.reason) == (False, "dnd")
    assert (await svc.check_outbound_allowed(db_session, tenant_id, "+919822222222")).allowed

    from packages.database.models import BlockedCaller

    db_session.add(BlockedCaller(tenant_id=tenant_id, e164_number="+919833333333"))
    await db_session.commit()
    assert (await svc.check_outbound_allowed(db_session, tenant_id, "+919833333333")).reason == "blocked"

    # TRAI quiet hours 21:00-09:00 in the contact's timezone
    night = datetime(2026, 3, 10, 17, 0, tzinfo=UTC)  # 22:30 IST
    morning = datetime(2026, 3, 10, 5, 0, tzinfo=UTC)  # 10:30 IST
    kw = dict(quiet_hours=("21:00", "09:00"), window=("00:00", "00:00"), tz_name="Asia/Kolkata")
    assert (await svc.check_outbound_allowed(db_session, tenant_id, "+919844444444", now=night, **kw)).reason == "outside_call_window"
    assert (await svc.check_outbound_allowed(db_session, tenant_id, "+919844444444", now=morning, **kw)).allowed

    entry_id = (await client.get(f"{BASE}/dnd", headers=headers)).json()[0]["id"]
    assert (await client.delete(f"{BASE}/dnd/{entry_id}", headers=headers)).status_code == 204


# --- SIP trunks --------------------------------------------------------------------------------------


async def test_sip_trunk_crud_and_sync_without_credentials(client: AsyncClient) -> None:
    headers = await _signup(client, "sip-co")
    created = await client.post(
        f"{BASE}/sip-trunks", headers=headers,
        json={"label": "Primary", "termination_uri": "acme.pstn.twilio.com", "caller_id": "+911234567890"},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    trunk = created.json()
    assert trunk["sync_status"] == "unsynced"

    patched = await client.patch(f"{BASE}/sip-trunks/{trunk['id']}", headers=headers, json={"label": "Main"})
    assert patched.json()["label"] == "Main"

    sync = await client.post(f"{BASE}/sip-trunks/{trunk['id']}/sync", headers=headers)
    assert sync.status_code == 400
    assert "Twilio credentials are not configured" in sync.json()["detail"]

    assert (await client.delete(f"{BASE}/sip-trunks/{trunk['id']}", headers=headers)).status_code == 204


async def test_sip_trunk_sync_calls_twilio(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import telephony_plus as api
    from app.integrations.credentials import TwilioCredentials
    from app.integrations.twilio_plus import TrunkSyncResult

    headers = await _signup(client, "sip2-co")
    trunk = (
        await client.post(
            f"{BASE}/sip-trunks", headers=headers,
            json={"label": "Primary", "termination_uri": "acme.pstn.twilio.com"},
        )
    ).json()  # fmt: skip

    async def fake_creds(db, tenant_id):
        return TwilioCredentials(account_sid="ACtest", auth_token="tok")

    seen: dict = {}

    def fake_sync(**kwargs):
        seen.update(kwargs)
        return TrunkSyncResult("TKabc", "acme.pstn.twilio.com", None, False)

    monkeypatch.setattr(api, "get_twilio_credentials", fake_creds)
    monkeypatch.setattr(api.twilio_plus, "sync_elastic_sip_trunk", fake_sync)
    synced = await client.post(f"{BASE}/sip-trunks/{trunk['id']}/sync", headers=headers)
    assert synced.status_code == 200
    assert synced.json()["twilio_trunk_sid"] == "TKabc" and synced.json()["sync_status"] == "synced"
    assert seen["termination_uri"] == "acme.pstn.twilio.com"


# --- campaigns: dialer, dispositions, stats, CSV -----------------------------------------------------


async def _campaign_with_contacts(
    db: AsyncSession, slug: str, phones: list[str], extras: dict[str, dict] | None = None
) -> tuple[Campaign, list[CampaignContact]]:
    tenant_id = await _tenant_id(db, slug)
    db.add(PhoneNumber(tenant_id=tenant_id, e164_number="+15559990000"))
    campaign = Campaign(
        tenant_id=tenant_id, name="Spring", channel=CampaignChannel.VOICE, status=CampaignStatus.RUNNING
    )
    db.add(campaign)
    await db.flush()
    contacts = []
    for phone in phones:
        customer = Customer(
            tenant_id=tenant_id, name=f"C {phone}", phone=phone, extra=(extras or {}).get(phone, {})
        )
        db.add(customer)
        await db.flush()
        contact = CampaignContact(tenant_id=tenant_id, campaign_id=campaign.id, customer_id=customer.id)
        db.add(contact)
        db.add(
            ConsentRecord(
                tenant_id=tenant_id,
                customer_id=customer.id,
                subject_phone=phone,
                purpose="marketing",
                status="granted",
                source="test",
                granted_at=datetime.now(UTC),
            )
        )
        contacts.append(contact)
    await db.commit()
    return campaign, contacts


@pytest.fixture
def fake_dialer_io(monkeypatch: pytest.MonkeyPatch):
    placed: list[str] = []
    rescheduled: list[dict] = []

    async def fake_place(db, campaign, phone_number, customer):
        call = Call(
            tenant_id=campaign.tenant_id,
            customer_id=customer.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.RINGING,
            from_number="+15559990000",
            to_number=customer.phone,
        )
        db.add(call)
        await db.flush()
        placed.append(customer.phone)
        return call

    monkeypatch.setattr(campaign_tasks, "_place_campaign_call", fake_place)
    monkeypatch.setattr(
        campaign_tasks.run_campaign_task, "apply_async", lambda **kw: rescheduled.append(kw)
    )
    return placed, rescheduled


async def test_dialer_pass_paces_enforces_dnd_and_windows_then_retries(
    client: AsyncClient, db_session: AsyncSession, fake_dialer_io
) -> None:
    placed, rescheduled = fake_dialer_io
    await _signup(client, "dial-co")
    phones = ["+919800000001", "+919800000002", "+919800000003", "+919800000004", "+919800000005"]
    campaign, contacts = await _campaign_with_contacts(db_session, "dial-co", phones)
    db_session.add(DndEntry(tenant_id=campaign.tenant_id, e164_number=phones[0]))
    ds = CampaignDialerSettings(
        tenant_id=campaign.tenant_id, campaign_id=campaign.id, mode="progressive",
        max_concurrent=2, call_window_start="00:00", call_window_end="00:00",
        quiet_hours_start="00:00", quiet_hours_end="00:00", retry_schedule_minutes=[30], max_attempts=2,
    )  # fmt: skip
    db_session.add(ds)
    await db_session.commit()

    await campaign_tasks._run_dialer_pass(db_session, campaign, ds, PhoneNumber(e164_number="+15559990000"))

    # DND contact skipped; progressive with max_concurrent=2 places only two calls
    assert placed == [phones[1], phones[2]]
    assert len(rescheduled) == 1 and rescheduled[0]["countdown"] == campaign_tasks.DIALER_POLL_SECONDS
    await db_session.refresh(contacts[0])
    assert contacts[0].status == CampaignContactStatus.FAILED and contacts[0].result == {"skipped": "dnd"}

    # a second pass while both calls are ringing places nothing
    await campaign_tasks._run_dialer_pass(db_session, campaign, ds, PhoneNumber(e164_number="+15559990000"))
    assert placed == [phones[1], phones[2]]

    # first call connects, second never answers -> dispositions + retry scheduled
    by_phone = {
        c.to_number: c for c in (await db_session.execute(select(Call))).scalars().all()
    }
    by_phone[phones[1]].status = CallStatus.COMPLETED
    by_phone[phones[1]].duration_seconds = 42
    by_phone[phones[2]].status = CallStatus.NO_ANSWER
    await db_session.commit()
    await campaign_tasks._run_dialer_pass(db_session, campaign, ds, PhoneNumber(e164_number="+15559990000"))

    outcomes = {
        o.disposition
        for o in (await db_session.execute(select(CampaignOutcome))).scalars().all()
    }
    assert outcomes == {"answered", "no_answer"}
    await db_session.refresh(contacts[1])
    await db_session.refresh(contacts[2])
    assert contacts[1].status == CampaignContactStatus.COMPLETED
    assert contacts[2].status == CampaignContactStatus.PENDING  # retry after 30 minutes
    state = (
        await db_session.execute(
            select(CampaignContactState).where(CampaignContactState.contact_id == contacts[2].id)
        )
    ).scalar_one()
    assert state.next_attempt_at is not None
    assert timedelta(minutes=29) < state.next_attempt_at - datetime.now(UTC) <= timedelta(minutes=30)
    assert len(placed) == 4  # the freed slots were used for the next contacts in that pass


async def test_dialer_defers_contacts_outside_their_timezone_window(
    client: AsyncClient, db_session: AsyncSession, fake_dialer_io
) -> None:
    placed, _ = fake_dialer_io
    await _signup(client, "tz-co")
    in_india, in_nyc = "+919800000011", "+14155550111"
    campaign, contacts = await _campaign_with_contacts(
        db_session, "tz-co", [in_india, in_nyc], extras={in_nyc: {"state": "NY"}}
    )
    ds = CampaignDialerSettings(
        tenant_id=campaign.tenant_id, campaign_id=campaign.id, mode="power", pacing_ratio=2.0,
        max_concurrent=10, call_window_start="09:00", call_window_end="21:00",
        quiet_hours_start="21:00", quiet_hours_end="09:00",
    )  # fmt: skip
    db_session.add(ds)
    await db_session.commit()

    # Freeze "now" at a moment when it is day in India but ~02:00 in New York.
    fixed = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)  # 11:30 IST, 02:00 EDT

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    import app.worker.campaign_tasks as ct

    ct_datetime = ct.datetime
    ct.datetime = FrozenDatetime
    try:
        await campaign_tasks._run_dialer_pass(db_session, campaign, ds, PhoneNumber(e164_number="+15559990000"))
    finally:
        ct.datetime = ct_datetime
    assert placed == [in_india]
    await db_session.refresh(contacts[1])
    assert contacts[1].status == CampaignContactStatus.PENDING  # deferred, not failed


async def test_disposition_callback_qualified_dnc_stats_and_csv(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _signup(client, "disp-co")
    campaign, contacts = await _campaign_with_contacts(
        db_session, "disp-co", ["+919800000021", "+919800000022", "+919800000023"]
    )
    cid = campaign.id

    put = await client.put(
        f"{BASE}/campaigns/{cid}/dialer-settings", headers=headers,
        json={"mode": "predictive", "max_concurrent": 8, "retry_schedule_minutes": [15, 60]},
    )  # fmt: skip
    assert put.status_code == 200 and put.json()["configured"] is True
    got = (await client.get(f"{BASE}/campaigns/{cid}/dialer-settings", headers=headers)).json()
    assert got["mode"] == "predictive" and got["retry_schedule_minutes"] == [15, 60]
    assert (
        await client.put(
            f"{BASE}/campaigns/{cid}/dialer-settings", headers=headers, json={"call_window_start": "9am"}
        )
    ).status_code == 422

    url = lambda c: f"{BASE}/campaigns/{cid}/contacts/{c.id}/disposition"  # noqa: E731
    cb_at = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    assert (await client.post(url(contacts[0]), headers=headers, json={"disposition": "callback"})).status_code == 422
    r = await client.post(url(contacts[0]), headers=headers, json={"disposition": "callback", "callback_at": cb_at, "notes": "=bad()"})
    assert r.status_code == 201, r.text
    q = await client.post(url(contacts[1]), headers=headers, json={"disposition": "qualified", "notes": "wants demo"})
    assert q.status_code == 201 and q.json()["qualified"] is True
    d = await client.post(url(contacts[2]), headers=headers, json={"disposition": "dnc"})
    assert d.status_code == 201

    await db_session.refresh(contacts[0])
    assert contacts[0].status == CampaignContactStatus.PENDING
    state = (
        await db_session.execute(select(CampaignContactState).where(CampaignContactState.contact_id == contacts[0].id))
    ).scalar_one()
    assert state.next_attempt_at is not None

    # qualified lead -> CRM lead when crm tables exist, else a ticket (both are valid, one must exist)
    await db_session.refresh(contacts[1])
    lead = contacts[1].result["lead"]
    if lead["type"] == "ticket":
        assert (await db_session.execute(select(Ticket))).scalars().first() is not None
    assert lead["type"] in {"ticket", "crm_lead"}

    dnd = (await client.get(f"{BASE}/dnd", headers=headers)).json()
    assert [e["e164_number"] for e in dnd] == ["+919800000023"]

    stats = (await client.get(f"{BASE}/campaigns/{cid}/stats", headers=headers)).json()
    assert stats["attempts"] == 3 and stats["qualified"] == 1
    assert stats["dispositions"] == {"callback": 1, "qualified": 1, "dnc": 1}
    assert stats["answered"] == 3 and stats["scheduled_retries"] == 1
    assert stats["total_contacts"] == 3

    export = await client.get(f"{BASE}/campaigns/{cid}/export.csv", headers=headers)
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
    lines = export.text.strip().splitlines()
    assert lines[0].startswith("contact_id,name,phone")
    assert len(lines) == 4
    assert "'=bad()" in export.text  # formula injection neutralised
    assert "+919800000022" in export.text and "qualified" in export.text

    outcomes = (await client.get(f"{BASE}/campaigns/{cid}/outcomes", headers=headers)).json()
    assert len(outcomes) == 3

    # other tenants cannot touch it
    other = await _signup(client, "disp-other")
    assert (await client.get(f"{BASE}/campaigns/{cid}/stats", headers=other)).status_code == 404


async def test_preview_next_skips_dnd_contacts(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _signup(client, "prev-co")
    campaign, contacts = await _campaign_with_contacts(db_session, "prev-co", ["+919800000031", "+919800000032"])
    db_session.add(DndEntry(tenant_id=campaign.tenant_id, e164_number="+919800000031"))
    await db_session.commit()
    await client.put(
        f"{BASE}/campaigns/{campaign.id}/dialer-settings", headers=headers,
        json={"mode": "preview", "call_window_start": "00:00", "call_window_end": "00:00",
              "quiet_hours_start": "00:00", "quiet_hours_end": "00:00"},
    )  # fmt: skip
    nxt = (await client.get(f"{BASE}/campaigns/{campaign.id}/preview-next", headers=headers)).json()
    assert nxt["phone"] == "+919800000032"


async def test_a_contact_who_never_agreed_is_not_dialed(
    client: AsyncClient, db_session: AsyncSession, fake_dialer_io
) -> None:
    placed, _rescheduled = fake_dialer_io
    await _signup(client, "consent-co")
    campaign, contacts = await _campaign_with_contacts(db_session, "consent-co", ["+919800000041"])
    consent = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.tenant_id == campaign.tenant_id)
        )
    ).scalar_one()
    await db_session.delete(consent)
    ds = CampaignDialerSettings(
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        mode="progressive",
        max_concurrent=2,
        call_window_start="00:00",
        call_window_end="00:00",
        quiet_hours_start="00:00",
        quiet_hours_end="00:00",
    )
    db_session.add(ds)
    await db_session.commit()
    await campaign_tasks._run_dialer_pass(
        db_session, campaign, ds, PhoneNumber(e164_number="+15559990000")
    )
    assert placed == []
    await db_session.refresh(contacts[0])
    assert contacts[0].result == {"skipped": "no_consent"}
