"""DPDP controls: privacy notice and officer, consent ledger, requests from
people about their data, erasure, retention and the breach register."""

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from app.services import privacy as svc
from app.services import recordings
from app.services.platform_retention import purge_tenant
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    ConsentRecord,
    CrmLead,
    Customer,
    DndEntry,
    PrivacyRequest,
    RetentionPolicy,
    Tenant,
    WidgetLead,
)
from packages.database.session import session_scope
from tests.test_call_control import _internal_headers, _make_in_progress_call

TEXT = {"Content-Type": "text/plain", "Origin": "https://shop.test", "User-Agent": "Mozilla/5.0 Chrome/120.0"}
ACCEPT = {"privacy_accepted": True, "adult_confirmed": True}
VISITOR = {"name": "Sam", "phone": "+919699234605", "email": "sam@example.com", "interest": "Facial"}
RECORDING = "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE" + "a" * 32 + ".mp3"


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": f"{slug} salon", "tenant_slug": slug, "full_name": "Owner",
              "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _channel(client: AsyncClient, auth: dict, **config) -> str:
    agent = await client.post("/api/v1/ai-agents", headers=auth, json={"name": f"Bot {uuid.uuid4().hex[:6]}"})
    resp = await client.put(
        "/api/v1/channels/settings/web_chat",
        headers=auth,
        json={"enabled": True, "ai_agent_id": agent.json()["id"], "config": {"lead_form": True, **config}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["public_key"]


async def _chat(client, key, **body):
    return await client.post(
        f"/api/v1/channels/webchat/{key}/session", content=json.dumps(body), headers=TEXT
    )


async def _tenant(slug: str) -> Tenant:
    async with session_scope() as db:
        return (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()


@pytest.fixture
def provider(monkeypatch):
    """Fakes the telephony provider: records deletions, can be made to fail."""
    state = {"deleted": [], "fail": False}

    async def creds(db, tenant_id):
        return None

    def delete(*, recording_sid, credentials=None):
        if state["fail"]:
            raise RuntimeError("provider down")
        state["deleted"].append(recording_sid)
        return True

    monkeypatch.setattr(recordings, "get_twilio_credentials", creds)
    monkeypatch.setattr(recordings.twilio_client, "delete_recording", delete)
    return state


# ---- notice and officer ---------------------------------------------------------------------


async def test_profile_defaults_and_notice_version(client: AsyncClient):
    auth = await _signup(client, "priv-prof")
    first = (await client.get("/api/v1/privacy/profile", headers=auth)).json()
    assert first["notice_version"] == 1 and first["officer_title"] == "Grievance Officer"
    assert first["public_url"].endswith("/privacy/priv-prof")
    assert any("18 or older" in line for line in first["notice_lines"])

    body = {k: first[k] for k in ("legal_name", "officer_name", "officer_title", "officer_email",
            "officer_phone", "address", "purposes", "retention_summary", "extra_notice",
            "call_disclosure_enabled", "call_disclosure_text", "request_sla_days")}
    # Changing only the disclosure wording or deadline does not change what people agreed to.
    same = await client.put("/api/v1/privacy/profile", headers=auth, json={**body, "request_sla_days": 20})
    assert same.json()["notice_version"] == 1 and same.json()["request_sla_days"] == 20
    changed = await client.put(
        "/api/v1/privacy/profile", headers=auth,
        json={**body, "officer_name": "Asha Rao", "officer_email": "asha@priv-prof.com"},
    )
    assert changed.json()["notice_version"] == 2
    assert any("Asha Rao" in line for line in changed.json()["notice_lines"])

    bad = await client.put("/api/v1/privacy/profile", headers=auth, json={**body, "purposes": []})
    assert bad.status_code == 422
    assert (await client.get("/api/v1/privacy/profile")).status_code == 401


# ---- chat consent ---------------------------------------------------------------------------


async def test_chat_needs_the_notice_accepted_and_age_confirmed(client: AsyncClient):
    auth = await _signup(client, "priv-chat")
    key = await _channel(client, auth, call_me=True)

    config = (await client.get(f"/api/v1/channels/webchat/{key}/config", headers=TEXT)).json()
    assert config["privacy"]["required"] and config["privacy"]["url"].endswith("/privacy/priv-chat")
    assert any("18 or older" in t for t in config["privacy"]["lines"])

    for missing in ({}, {"privacy_accepted": True}, {"adult_confirmed": True},
                    {"privacy_accepted": "yes", "adult_confirmed": True}):
        resp = await _chat(client, key, **VISITOR, **missing)
        assert resp.status_code == 422 and "privacy notice" in resp.json()["detail"]
    async with session_scope() as db:
        assert (await db.execute(select(Customer))).first() is None  # nothing stored without consent

    ok = await _chat(client, key, **VISITOR, **ACCEPT, notice_version=1)
    assert ok.status_code == 200
    async with session_scope() as db:
        record = (await db.execute(select(ConsentRecord))).scalar_one()
    assert (record.purpose, record.status, record.source) == ("enquiry", "granted", "web_chat")
    assert record.subject_phone == "+919699234605" and record.notice_version == 1
    assert len(record.notice_hash) == 64 and record.ip_address


async def test_a_business_can_turn_the_gate_off_and_a_call_request_is_recorded(client: AsyncClient, monkeypatch):
    auth = await _signup(client, "priv-off")
    key = await _channel(client, auth, privacy_consent=False, lead_form=False)
    config = (await client.get(f"/api/v1/channels/webchat/{key}/config", headers=TEXT)).json()
    assert config["privacy"] is None
    assert (await _chat(client, key)).status_code == 200
    async with session_scope() as db:
        assert (await db.execute(select(ConsentRecord))).first() is None


async def test_asking_for_a_call_adds_a_call_back_consent(client: AsyncClient, monkeypatch):
    from app.services import widget_leads as wl

    async def fake(db, tenant_id, *, to_number, ai_agent_id=None, from_number=None):
        call = Call(tenant_id=tenant_id, direction=CallDirection.OUTBOUND, status=CallStatus.RINGING,
                    to_number=to_number, from_number="+13613095598")
        db.add(call)
        await db.commit()
        return call

    monkeypatch.setattr(wl, "place_ai_outbound_call", fake)
    auth = await _signup(client, "priv-call")
    async with session_scope() as db:
        from packages.database.models import PhoneNumber
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "priv-call"))).scalar_one()
        db.add(PhoneNumber(tenant_id=tenant.id, e164_number="+15550001111"))
        await db.commit()
    key = await _channel(client, auth, call_me=True)
    token = (await _chat(client, key, **VISITOR, **ACCEPT)).json()["session_token"]
    resp = await client.post(
        f"/api/v1/channels/webchat/{key}/call-request",
        content=json.dumps({"session_token": token, "consent": True}), headers=TEXT,
    )
    assert resp.status_code == 200, resp.text
    async with session_scope() as db:
        purposes = sorted((await db.execute(select(ConsentRecord.purpose))).scalars().all())
    assert purposes == ["call_back", "enquiry"]


# ---- withdrawal -----------------------------------------------------------------------------


async def test_withdrawing_consent_stops_calls_at_once(client: AsyncClient):
    auth = await _signup(client, "priv-wd")
    key = await _channel(client, auth, call_me=True)
    await _chat(client, key, **VISITOR, **ACCEPT)
    async with session_scope() as db:
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "priv-wd"))).scalar_one()
        lead.consent_call = True
        await svc.record_consent(db, tenant.id, purposes=["call_back"], source="web_chat",
                                 business="x", phone=VISITOR["phone"])
        await db.commit()

    resp = await client.post(
        "/api/v1/privacy/public/priv-wd/withdraw", json={"phone": "+91 96992 34605", "purposes": ["call_back"]}
    )
    assert resp.status_code == 200
    async with session_scope() as db:
        statuses = {(c.purpose, c.status) for c in (await db.execute(select(ConsentRecord))).scalars()}
        assert ("call_back", "withdrawn") in statuses and ("enquiry", "granted") in statuses
        assert (await db.execute(select(DndEntry.e164_number))).scalars().all() == ["+919699234605"]
        assert (await db.execute(select(WidgetLead.consent_call))).scalar_one() is False
    # naming no purpose takes back everything; saying it twice changes nothing and does not fail
    assert (await client.post("/api/v1/privacy/public/priv-wd/withdraw", json={"phone": VISITOR["phone"]})).status_code == 200
    async with session_scope() as db:
        assert {c.status for c in (await db.execute(select(ConsentRecord))).scalars()} == {"withdrawn"}
    assert (await client.post("/api/v1/privacy/public/priv-wd/withdraw", json={"phone": VISITOR["phone"]})).status_code == 200
    assert (await client.post("/api/v1/privacy/public/priv-wd/withdraw", json={})).status_code == 422
    assert (await client.post("/api/v1/privacy/public/nobody-here/withdraw", json={"phone": "+911"})).status_code == 404


# ---- requests -------------------------------------------------------------------------------


async def test_public_request_is_tracked_to_a_due_date_and_status_needs_the_contact(client: AsyncClient):
    auth = await _signup(client, "priv-req")
    resp = await client.post(
        "/api/v1/privacy/public/priv-req/requests",
        json={"kind": "erasure", "name": "Sam", "email": "Sam@Example.com", "details": "Please delete me"},
    )
    assert resp.status_code == 201
    ref = resp.json()["reference"]
    assert ref.startswith("PR-")
    queue = (await client.get("/api/v1/privacy/requests", headers=auth)).json()
    assert queue[0]["reference"] == ref and queue[0]["state"] == "open" and queue[0]["subject_email"] == "sam@example.com"
    due = datetime.fromisoformat(queue[0]["due_at"])
    assert timedelta(days=29) < due - datetime.now(UTC) <= timedelta(days=30)

    status_url = "/api/v1/privacy/public/priv-req/requests/status"
    ok = await client.post(status_url, json={"reference": ref, "contact": "sam@example.com"})
    assert ok.status_code == 200 and ok.json()["status"] == "received"
    assert (await client.post(status_url, json={"reference": ref, "contact": "other@example.com"})).status_code == 404
    assert (await client.post(status_url, json={"reference": "PR-NOPE", "contact": "sam@example.com"})).status_code == 404

    # the form must say who and how to reach them; the bot trap is silently accepted
    assert (await client.post("/api/v1/privacy/public/priv-req/requests", json={"kind": "erasure", "name": "Sam"})).status_code == 422
    assert (await client.post("/api/v1/privacy/public/priv-req/requests", json={"kind": "sell", "name": "Sam", "email": "a@b.co"})).status_code == 422
    trap = await client.post("/api/v1/privacy/public/priv-req/requests",
                             json={"kind": "access", "name": "Bot", "email": "a@b.co", "website": "http://spam"})
    assert trap.status_code == 201 and len((await client.get("/api/v1/privacy/requests", headers=auth)).json()) == 1


async def test_overdue_requests_are_flagged_and_other_businesses_cannot_see_them(client: AsyncClient):
    auth = await _signup(client, "priv-late")
    other = await _signup(client, "priv-other")
    await client.post("/api/v1/privacy/public/priv-late/requests",
                      json={"kind": "access", "name": "Sam", "email": "s@example.com"})
    async with session_scope() as db:
        row = (await db.execute(select(PrivacyRequest))).scalar_one()
        row.due_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        request_id = str(row.id)
    listed = (await client.get("/api/v1/privacy/requests", headers=auth)).json()
    assert listed[0]["state"] == "overdue"
    checklist = (await client.get("/api/v1/privacy/checklist", headers=auth)).json()
    assert {i["key"]: i["ok"] for i in checklist["items"]}["requests_on_time"] is False

    assert (await client.get("/api/v1/privacy/requests", headers=other)).json() == []
    assert (await client.post(f"/api/v1/privacy/requests/{request_id}/verify", headers=other)).status_code == 404


async def _lead_with_everything(client, slug: str, provider):
    """A person with a customer record, a website enquiry, a CRM lead, a call with audio and a consent."""
    auth = await _signup(client, slug)
    key = await _channel(client, auth)
    await _chat(client, key, **VISITOR, **ACCEPT)
    async with session_scope() as db:
        customer = (await db.execute(select(Customer))).scalar_one()
        tenant = await db.scalar(select(Tenant).where(Tenant.slug == slug))
        db.add(Call(tenant_id=tenant.id, customer_id=customer.id, direction=CallDirection.INBOUND,
                    status=CallStatus.COMPLETED, recording_url=RECORDING))
        await db.commit()
    return auth


async def test_erasure_removes_everything_about_the_person_and_keeps_the_proof(client: AsyncClient, provider):
    auth = await _lead_with_everything(client, "priv-erase", provider)
    ref = (await client.post("/api/v1/privacy/public/priv-erase/requests",
           json={"kind": "erasure", "name": "Sam", "phone": "+91 96992 34605"})).json()["reference"]
    request_id = (await client.get("/api/v1/privacy/requests", headers=auth)).json()[0]["id"]

    # nobody's data is removed or shown until the officer has confirmed who is asking
    early = await client.post(f"/api/v1/privacy/requests/{request_id}/fulfil", headers=auth)
    assert early.status_code == 409
    assert (await client.get(f"/api/v1/privacy/requests/{request_id}/export", headers=auth)).status_code == 409

    await client.post(f"/api/v1/privacy/requests/{request_id}/verify", headers=auth)
    export = await client.get(f"/api/v1/privacy/requests/{request_id}/export", headers=auth)
    names = zipfile.ZipFile(io.BytesIO(export.content)).namelist()
    assert {"website_chat_enquiries.csv", "crm_leads.csv", "consents.csv", "calls.csv"} <= set(names)

    done = await client.post(f"/api/v1/privacy/requests/{request_id}/fulfil", headers=auth)
    assert done.status_code == 200 and done.json()["status"] == "completed" and done.json()["state"] == "done"
    assert provider["deleted"] == ["RE" + "a" * 32]  # the audio is gone at the provider, not just unlinked

    async with session_scope() as db:
        for model in (Customer, WidgetLead, CrmLead, Call):
            assert (await db.execute(select(model))).first() is None, model
        consent = (await db.execute(select(ConsentRecord))).scalar_one()
        assert consent.purpose == "enquiry" and consent.notice_hash  # proof stays...
        assert (consent.subject_name, consent.subject_phone, consent.subject_email, consent.customer_id) == (None,) * 4
    again = await client.post("/api/v1/privacy/public/priv-erase/requests/status", json={"reference": ref, "contact": "+919699234605"})
    assert again.json()["status"] == "completed" and "erased" in again.json()["resolution"]
    assert (await client.post(f"/api/v1/privacy/requests/{request_id}/fulfil", headers=auth)).status_code == 409


async def test_erasure_reports_audio_the_provider_would_not_delete(client: AsyncClient, provider):
    auth = await _lead_with_everything(client, "priv-fail", provider)
    provider["fail"] = True
    await client.post("/api/v1/privacy/requests", headers=auth,
                      json={"kind": "erasure", "name": "Sam", "phone": VISITOR["phone"]})
    request_id = (await client.get("/api/v1/privacy/requests", headers=auth)).json()[0]["id"]
    await client.post(f"/api/v1/privacy/requests/{request_id}/verify", headers=auth)
    done = await client.post(f"/api/v1/privacy/requests/{request_id}/fulfil", headers=auth)
    assert done.status_code == 200 and "could not be deleted at the provider" in done.json()["resolution"]


async def test_other_request_kinds_need_a_note_and_can_be_rejected(client: AsyncClient):
    auth = await _signup(client, "priv-kinds")
    await client.post("/api/v1/privacy/requests", headers=auth,
                      json={"kind": "correction", "name": "Sam", "email": "s@example.com", "details": "Wrong spelling"})
    await client.post("/api/v1/privacy/requests", headers=auth,
                      json={"kind": "grievance", "name": "Ria", "email": "r@example.com"})
    rows = (await client.get("/api/v1/privacy/requests", headers=auth)).json()
    correction = next(r for r in rows if r["kind"] == "correction")
    grievance = next(r for r in rows if r["kind"] == "grievance")

    await client.post(f"/api/v1/privacy/requests/{correction['id']}/verify", headers=auth)
    assert (await client.post(f"/api/v1/privacy/requests/{correction['id']}/fulfil", headers=auth)).status_code == 422
    ok = await client.post(f"/api/v1/privacy/requests/{correction['id']}/fulfil", headers=auth,
                           json={"resolution": "Corrected the spelling"})
    assert ok.json()["resolution"] == "Corrected the spelling"

    rejected = await client.post(f"/api/v1/privacy/requests/{grievance['id']}/reject", headers=auth,
                                 json={"resolution": "Not about this business"})
    assert rejected.json()["status"] == "rejected"
    assert (await client.post(f"/api/v1/privacy/requests/{grievance['id']}/reject", headers=auth,
                              json={"resolution": "again"})).status_code == 409


async def test_erasing_someone_who_is_not_a_customer_still_removes_their_enquiries(client: AsyncClient):
    auth = await _signup(client, "priv-nocust")
    key = await _channel(client, auth)
    await _chat(client, key, **VISITOR, **ACCEPT)
    async with session_scope() as db:  # the visit is on file but never became a customer
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        lead.customer_id = None
        await db.commit()
    request = (await client.post("/api/v1/privacy/requests", headers=auth,
               json={"kind": "erasure", "name": "Sam", "email": "sam@example.com"})).json()
    async with session_scope() as db:
        row = await db.get(PrivacyRequest, uuid.UUID(request["id"]))
        row.customer_id = None
        await db.commit()
    await client.post(f"/api/v1/privacy/requests/{request['id']}/verify", headers=auth)
    assert (await client.post(f"/api/v1/privacy/requests/{request['id']}/fulfil", headers=auth)).status_code == 200
    async with session_scope() as db:
        assert (await db.execute(select(WidgetLead))).first() is None


# ---- retention ------------------------------------------------------------------------------


async def test_retention_deletes_audio_and_old_leads_and_retries_what_failed(client: AsyncClient, provider):
    auth = await _lead_with_everything(client, "priv-ret", provider)
    now = datetime.now(UTC)
    async with session_scope() as db:
        tenant = await db.scalar(select(Tenant).where(Tenant.slug == "priv-ret"))
        call = (await db.execute(select(Call))).scalar_one()
        call.ended_at = now - timedelta(days=40)
        lead = (await db.execute(select(WidgetLead))).scalar_one()
        lead.customer_id = None  # a visit that never became a customer
        lead.created_at = now - timedelta(days=40)
        db.add(RetentionPolicy(tenant_id=tenant.id, enabled=True, recording_days=30, lead_days=30,
                               last_purge_summary={}))
        await db.commit()

    provider["fail"] = True
    async with session_scope() as db:
        policy = (await db.execute(select(RetentionPolicy))).scalar_one()
        first = await purge_tenant(db, policy)
        await db.commit()
    assert first["recordings_failed"] == 1 and first["recordings_cleared"] == 0 and first["leads_deleted"] == 1
    async with session_scope() as db:
        assert (await db.execute(select(Call.recording_url))).scalar_one() == RECORDING  # kept, so it retries

    provider["fail"] = False
    async with session_scope() as db:
        policy = (await db.execute(select(RetentionPolicy))).scalar_one()
        second = await purge_tenant(db, policy)
        await db.commit()
    assert second["recordings_cleared"] == 1 and provider["deleted"] == ["RE" + "a" * 32]
    async with session_scope() as db:
        assert (await db.execute(select(Call.recording_url))).scalar_one() is None

    put = await client.put("/api/v1/security/retention" if False else "/api/v1/platform/security/retention",
                           headers=auth, json={"enabled": True, "lead_days": 90})
    assert put.status_code == 200 and put.json()["lead_days"] == 90


def test_recording_links_that_are_not_the_providers_need_no_provider_call():
    assert recordings.recording_sid(RECORDING) == "RE" + "a" * 32
    assert recordings.recording_sid("https://example.com/x.mp3") is None
    assert recordings.recording_sid(None) is None


# ---- breach register ------------------------------------------------------------------------


async def test_a_breach_starts_a_72_hour_clock_and_drafts_the_notices(client: AsyncClient):
    auth = await _signup(client, "priv-breach")
    created = await client.post("/api/v1/privacy/breaches", headers=auth, json={
        "title": "Exported list emailed to the wrong person",
        "description": "A customer export was sent to a wrong address.",
        "data_categories": ["names", "phone numbers"], "approx_people": 40,
        "likely_impact": "Unwanted calls", "mitigation": "Asked the recipient to delete it",
    })
    assert created.status_code == 201
    breach = created.json()
    found = datetime.fromisoformat(breach["discovered_at"])
    assert datetime.fromisoformat(breach["report_due_at"]) - found == timedelta(hours=72)
    assert breach["state"] == "report_due"

    draft = (await client.get(f"/api/v1/privacy/breaches/{breach['id']}/draft", headers=auth)).json()
    assert "names, phone numbers" in draft["to_people"] and "Unwanted calls" in draft["to_people"]
    assert draft["to_board"]["nature_extent_timing_location"]["approx_people"] == 40

    patched = await client.patch(f"/api/v1/privacy/breaches/{breach['id']}", headers=auth,
                                 json={"board_notified": True, "board_reference": "DPB-1", "principals_notified": True,
                                       "principals_notified_how": "Email"})
    assert patched.json()["state"] == "reported" and patched.json()["board_reference"] == "DPB-1"
    closed = await client.patch(f"/api/v1/privacy/breaches/{breach['id']}", headers=auth, json={"closed": True})
    assert closed.json()["state"] == "closed" and closed.json()["closed_at"]


async def test_a_late_breach_report_is_overdue_and_shown_in_the_checklist(client: AsyncClient):
    auth = await _signup(client, "priv-late-b")
    old = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    created = await client.post("/api/v1/privacy/breaches", headers=auth, json={
        "title": "Lost laptop", "description": "A laptop with the CRM open was lost.", "discovered_at": old})
    assert created.json()["state"] == "overdue"
    items = {i["key"]: i for i in (await client.get("/api/v1/privacy/checklist", headers=auth)).json()["items"]}
    assert items["breaches_on_time"]["ok"] is False and "1 overdue" in items["breaches_on_time"]["hint"]
    other = await _signup(client, "priv-late-c")
    assert (await client.get("/api/v1/privacy/breaches", headers=other)).json() == []


# ---- the checklist and the call disclosure --------------------------------------------------


async def test_checklist_turns_green_as_the_business_does_the_work(client: AsyncClient):
    auth = await _signup(client, "priv-check")
    before = (await client.get("/api/v1/privacy/checklist", headers=auth)).json()
    ok_before = {i["key"]: i["ok"] for i in before["items"]}
    assert ok_before["officer"] is False and ok_before["retention"] is False and before["score"] < before["total"]

    profile = (await client.get("/api/v1/privacy/profile", headers=auth)).json()
    body = {k: profile[k] for k in ("legal_name", "officer_name", "officer_title", "officer_email",
            "officer_phone", "address", "purposes", "retention_summary", "extra_notice",
            "call_disclosure_enabled", "call_disclosure_text", "request_sla_days")}
    await client.put("/api/v1/privacy/profile", headers=auth,
                     json={**body, "officer_email": "dpo@priv-check.com", "address": "1 MG Road, Pune"})
    await client.put("/api/v1/platform/security/retention", headers=auth,
                     json={"enabled": True, "transcript_days": 90})
    after = {i["key"]: i["ok"] for i in (await client.get("/api/v1/privacy/checklist", headers=auth)).json()["items"]}
    assert after["officer"] and after["legal_name"] and after["retention"]


async def test_callers_hear_that_it_is_an_ai_and_that_calls_may_be_recorded(client: AsyncClient, db_session):
    from tests.test_call_control import _auth
    from tests.test_call_control import _signup as call_signup

    tokens = await call_signup(client, slug="priv-greet", email="owner@priv-greet.com")
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot", "greeting": "Hello, how can I help?"})
    call = await _make_in_progress_call(db_session, tenant_slug="priv-greet", agent_id=agent.json()["id"], started_seconds_ago=3)

    async def greeting():
        resp = await client.get(f"/api/v1/internal/calls/{call.id}/config", headers=_internal_headers())
        return resp.json()["greeting"]

    assert await greeting() == "This call is with an AI assistant. Hello, how can I help?"  # not recording yet
    await client.patch("/api/v1/tenants/me", headers=_auth(tokens), json={"call_recording_enabled": True})
    assert (await greeting()).startswith("This call is with an AI assistant and may be recorded")

    profile = (await client.get("/api/v1/privacy/profile", headers=_auth(tokens))).json()
    body = {k: profile[k] for k in ("legal_name", "officer_name", "officer_title", "officer_email",
            "officer_phone", "address", "purposes", "retention_summary", "extra_notice",
            "call_disclosure_text", "request_sla_days")}
    await client.put("/api/v1/privacy/profile", headers=_auth(tokens), json={**body, "call_disclosure_enabled": False})
    assert (await greeting()).startswith("This call is with an AI assistant and may be recorded")
    await client.patch("/api/v1/tenants/me", headers=_auth(tokens), json={"call_recording_enabled": False})
    assert await greeting() == "Hello, how can I help?"


async def test_public_notice_page_data(client: AsyncClient):
    await _signup(client, "priv-pub")
    notice = (await client.get("/api/v1/privacy/public/priv-pub/notice")).json()
    assert notice["version"] == 1 and notice["business"] == "priv-pub salon" and notice["sla_days"] == 30
    assert len(notice["hash"]) == 64 and notice["purposes"]
    assert (await client.get("/api/v1/privacy/public/missing/notice")).status_code == 404
    _ = AIAgent  # imported for model registration


async def test_two_first_requests_at_once_create_one_profile_and_neither_fails(client: AsyncClient):
    """The privacy page asks for the profile from several places on a business's first visit."""
    import asyncio

    await _signup(client, "priv-race")
    tenant = await _tenant("priv-race")

    async def first_visit():
        async with session_scope() as db:
            row = await svc.get_profile(db, tenant.id)
            await db.commit()
            return row.id

    ids = await asyncio.gather(*(first_visit() for _ in range(6)))
    assert len(set(ids)) == 1
    async with session_scope() as db:
        from sqlalchemy import func as f
        from packages.database.models import PrivacyProfile
        assert (await db.execute(select(f.count()).select_from(PrivacyProfile))).scalar_one() == 1
