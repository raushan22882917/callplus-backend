"""What the per-tenant OAuth tokens are used for: calendar mirroring and busy
blocking, CRM push, Slack notifications, WhatsApp Embedded Signup. All provider
HTTP is mocked; the calendar hook must never break booking."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from app.core.crypto import decrypt_secret
from app.integrations.oauth.tokens import get_connection
from app.services import calendar_sync
from app.services import operations as svc
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, Tenant, WhatsAppAccount
from packages.database.models.crm import CrmLead
from packages.database.models.operations import Appointment, AppointmentResource, BookableService
from tests.test_call_control import _auth, _signup
from tests.test_oauth import (  # noqa: F401 - fixtures
    MockProviders,
    creds,
    providers,
    seed_connection,
    tenant_id_of,
)

HOURS = {d: [["09:00", "12:00"], ["13:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
NEXT_MONDAY = date.today() + timedelta(days=(7 - date.today().weekday()))


def monday_at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(NEXT_MONDAY, datetime.min.time(), tzinfo=UTC).replace(hour=hour, minute=minute)


async def make_tenant(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, BookableService]:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Jane", phone="+15550002222", email="jane@x.test")
    service = BookableService(tenant_id=tenant.id, name="Cleaning", duration_minutes=60, price=500)
    resource = AppointmentResource(tenant_id=tenant.id, name="Chair 1", kind="chair", working_hours=HOURS)
    db.add_all([customer, service, resource])
    await db.commit()
    return tenant.id, customer.id, service


GCAL = "https://www.googleapis.com/calendar/v3/calendars/"


def google_calendar_mocks(mock: MockProviders) -> None:
    mock.add("POST", GCAL + "primary/events", {"id": "evt-1"})
    mock.add("PATCH", GCAL + "primary/events/evt-1", {"id": "evt-1"})
    mock.add("DELETE", GCAL + "primary/events/evt-1", {}, status=204)


# ---------------------------------------------------------------- calendar mirroring


async def test_booking_reschedule_cancel_mirror_to_google_calendar(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)
    await seed_connection(db_session, tenant_id, "google")
    google_calendar_mocks(providers)

    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(10)
    )
    await db_session.commit()
    created = providers.calls_to("POST", GCAL + "primary/events")
    assert len(created) == 1
    body = json.loads(created[0].content)
    assert body["summary"] == "Cleaning - Jane" and "Phone: +15550002222" in body["description"]
    assert body["start"]["dateTime"].startswith(monday_at(10).strftime("%Y-%m-%dT10:00:00"))
    assert created[0].headers["Authorization"] == "Bearer a-1"
    await db_session.refresh(appt)
    assert appt.calendar_events == {"google": "evt-1"}

    await svc.reschedule_appointment(db_session, tenant_id, appt.id, start_at=monday_at(14))
    await db_session.commit()
    patched = providers.calls_to("PATCH", GCAL + "primary/events/evt-1")
    assert len(patched) == 1
    assert json.loads(patched[0].content)["start"]["dateTime"].startswith(
        monday_at(14).strftime("%Y-%m-%dT14:00:00")
    )

    await svc.cancel_appointment(db_session, tenant_id, appt.id, "changed mind")
    await db_session.commit()
    assert len(providers.calls_to("DELETE", GCAL + "primary/events/evt-1")) == 1
    await db_session.refresh(appt)
    assert appt.status == "cancelled" and appt.calendar_events == {}


async def test_booking_mirrors_to_outlook_selected_calendar(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)
    await seed_connection(
        db_session, tenant_id, "microsoft",
        extra={"selected": {"calendar_id": "AAMk-cal", "calendar_name": "Clinic"}},
    )
    base = "https://graph.microsoft.com/v1.0/me/"
    providers.add("POST", base + "calendars/AAMk-cal/events", {"id": "ms-evt-1"})
    providers.add("PATCH", base + "events/ms-evt-1", {"id": "ms-evt-1"})
    providers.add("DELETE", base + "events/ms-evt-1", {}, status=204)
    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(9)
    )
    payload = json.loads(providers.calls[0].content)
    assert payload["subject"] == "Cleaning - Jane" and payload["start"]["timeZone"] == "UTC"
    assert appt.calendar_events == {"microsoft": "ms-evt-1"}
    await svc.reschedule_appointment(db_session, tenant_id, appt.id, start_at=monday_at(11))
    await svc.cancel_appointment(db_session, tenant_id, appt.id)
    assert providers.calls_to("PATCH", base + "events/ms-evt-1") and providers.calls_to("DELETE", base + "events/ms-evt-1")


async def test_no_calendar_connected_means_no_http_and_booking_works(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)
    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(10)
    )
    assert appt.calendar_events == {} and providers.calls == []


@pytest.mark.parametrize("failure", ["500", "network", "refresh_rejected", "garbage"])
async def test_calendar_hook_never_breaks_booking(
    db_session: AsyncSession, providers: MockProviders, creds, failure: str
) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)
    conn = await seed_connection(db_session, tenant_id, "google")
    if failure == "500":
        providers.add("POST", GCAL + "primary/events", {"error": "down"}, status=500)
    elif failure == "network":
        def boom(request):
            raise httpx.ConnectError("no route", request=request)
        providers.add("POST", GCAL + "primary/events", handler=boom)
    elif failure == "refresh_rejected":
        conn.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db_session.commit()
        providers.add("POST", "https://oauth2.googleapis.com/token", {"error": "invalid_grant"}, status=400)
    else:
        providers.add("POST", GCAL + "primary/events", handler=lambda r: httpx.Response(200, content=b"<html>"))

    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(10)
    )
    await db_session.commit()
    assert appt.id and appt.status == "booked" and appt.calendar_events == {}
    # and cancelling / rescheduling afterwards is just as safe
    await svc.reschedule_appointment(db_session, tenant_id, appt.id, start_at=monday_at(14))
    await svc.cancel_appointment(db_session, tenant_id, appt.id)
    await db_session.commit()
    fresh = await db_session.get(Appointment, appt.id)
    assert fresh.status == "cancelled"


async def test_calendar_sync_swallows_even_unexpected_errors(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)

    async def explode(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(calendar_sync, "calendar_connections", explode)
    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(10)
    )
    assert appt.status == "booked"
    await calendar_sync.sync_appointment(db_session, appt, "create")  # must not raise
    assert await calendar_sync.external_busy(db_session, tenant_id, monday_at(0), monday_at(23)) == []


async def test_expired_calendar_event_is_recreated(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)
    await seed_connection(db_session, tenant_id, "google")
    providers.add("POST", GCAL + "primary/events", {"id": "evt-2"})
    providers.add("PATCH", GCAL + "primary/events/evt-old", {"error": "gone"}, status=410)
    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(10)
    )
    appt.calendar_events = {"google": "evt-old"}
    await svc.reschedule_appointment(db_session, tenant_id, appt.id, start_at=monday_at(15))
    assert appt.calendar_events == {"google": "evt-2"}


# ---------------------------------------------------------------- busy blocking


async def test_busy_times_block_slots_only_when_opted_in(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tenant_id, customer_id, service = await make_tenant(db_session)
    conn = await seed_connection(db_session, tenant_id, "google", extra={"selected": {"calendar_id": "primary"}})
    busy = [{"start": monday_at(9).isoformat(), "end": monday_at(11).isoformat()}]
    providers.add("POST", "https://www.googleapis.com/calendar/v3/freeBusy", {"calendars": {"primary": {"busy": busy}}})

    def hours(slots):
        return [s["start"].hour for s in slots]

    off = await svc.free_slots(db_session, tenant_id, NEXT_MONDAY, service.id, step_minutes=60)
    assert hours(off) == [9, 10, 11, 13, 14, 15, 16] and providers.calls == []

    conn.extra = {"selected": {"calendar_id": "primary", "block_busy": True}}
    await db_session.commit()
    on = await svc.free_slots(db_session, tenant_id, NEXT_MONDAY, service.id, step_minutes=60)
    assert hours(on) == [11, 13, 14, 15, 16]
    assert json.loads(providers.calls[0].content)["items"] == [{"id": "primary"}]

    # provider down: availability falls back to our own data, never errors
    providers.routes.clear()
    providers.add("POST", "https://www.googleapis.com/calendar/v3/freeBusy", {"error": "x"}, status=503)
    assert hours(await svc.free_slots(db_session, tenant_id, NEXT_MONDAY, service.id, step_minutes=60)) == hours(off)


async def test_outlook_busy_times_ignore_free_and_cancelled(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tenant_id, _, service = await make_tenant(db_session)
    await seed_connection(db_session, tenant_id, "microsoft", extra={"selected": {"block_busy": True}})
    iso = lambda h: monday_at(h).strftime("%Y-%m-%dT%H:%M:%S.0000000")  # noqa: E731
    providers.add(
        "GET", "https://graph.microsoft.com/v1.0/me/calendarView",
        {"value": [
            {"start": {"dateTime": iso(9)}, "end": {"dateTime": iso(10)}, "showAs": "busy"},
            {"start": {"dateTime": iso(10)}, "end": {"dateTime": iso(11)}, "showAs": "free"},
            {"start": {"dateTime": iso(11)}, "end": {"dateTime": iso(12)}, "showAs": "busy", "isCancelled": True},
        ]},
    )
    slots = await svc.free_slots(db_session, tenant_id, NEXT_MONDAY, service.id, step_minutes=60)
    assert [s["start"].hour for s in slots] == [10, 11, 13, 14, 15, 16]


# ---------------------------------------------------------------- calendar select endpoints


async def test_calendar_resources_and_select_via_api(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="cal-co", email="o@calco.com")
    tid = await tenant_id_of(db_session, "cal-co")
    await seed_connection(db_session, tid, "google")
    providers.add(
        "GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        {"items": [{"id": "me@x.test", "summary": "Me", "primary": True, "accessRole": "owner"},
                   {"id": "team@group", "summary": "Team", "accessRole": "writer"}]},
    )
    res = await client.get("/api/v1/oauth/google/resources", headers=_auth(tokens))
    assert [(i["id"], i["primary"]) for i in res.json()["items"]] == [("me@x.test", True), ("team@group", False)]
    sel = await client.post(
        "/api/v1/oauth/google/select", headers=_auth(tokens),
        json={"resource_id": "team@group", "block_busy": True},
    )
    assert sel.json()["selected"] == {"calendar_id": "team@group", "calendar_name": "Team", "block_busy": True}
    bad = await client.post("/api/v1/oauth/google/select", headers=_auth(tokens), json={"resource_id": "ghost"})
    assert bad.status_code == 400
    # events now go to the chosen calendar
    providers.add("POST", GCAL + "team%40group/events", {"id": "evt-t"})
    tenant_id, customer_id, service = await make_tenant(db_session)
    await seed_connection(db_session, tenant_id, "google", extra={"selected": {"calendar_id": "team@group"}})
    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=monday_at(10)
    )
    assert appt.calendar_events == {"google": "evt-t"}


async def test_resources_for_provider_without_resources(
    client: AsyncClient, creds, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="nr-co", email="o@nrco.com")
    await seed_connection(db_session, await tenant_id_of(db_session, "nr-co"), "hubspot")
    res = await client.get("/api/v1/oauth/hubspot/resources", headers=_auth(tokens))
    assert res.json() == {"kind": "none", "items": [], "selected": None}


# ---------------------------------------------------------------- CRM push


async def test_new_lead_is_upserted_into_hubspot_and_failure_is_harmless(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="crm-co", email="o@crmco.com")
    await seed_connection(db_session, await tenant_id_of(db_session, "crm-co"), "hubspot")
    providers.add("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert", {"results": []})
    resp = await client.post(
        "/api/v1/crm/leads", headers=_auth(tokens),
        json={"name": "Ada Lovelace", "email": "Ada@Example.com", "phone": "+15551230000"},
    )
    assert resp.status_code == 201, resp.text
    call = providers.calls_to("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert")[0]
    sent = json.loads(call.content)["inputs"][0]
    assert sent["id"] == "ada@example.com" and sent["properties"]["firstname"] == "Ada"
    assert call.headers["Authorization"] == "Bearer a-1"
    lead = (await db_session.execute(select(CrmLead))).scalar_one()
    assert lead.custom_fields["crm_sync"]["hubspot"]["ok"] is True

    providers.routes.clear()
    providers.add("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert", {"message": "x"}, status=500)
    resp = await client.post("/api/v1/crm/leads", headers=_auth(tokens), json={"name": "Bob", "email": "bob@x.test"})
    assert resp.status_code == 201  # lead still created
    db_session.expire_all()
    bob = (await db_session.execute(select(CrmLead).where(CrmLead.name == "Bob"))).scalar_one()
    assert bob.custom_fields["crm_sync"]["hubspot"]["ok"] is False


async def test_manual_push_to_salesforce_and_zoho(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="push-co", email="o@pushco.com")
    tid = await tenant_id_of(db_session, "push-co")
    lead = (await client.post("/api/v1/crm/leads", headers=_auth(tokens), json={
        "name": "Grace Hopper", "email": "grace@navy.test", "phone": "+15559990000"})).json()
    url = f"/api/v1/crm/leads/{lead['id']}/push-to-crm"

    none = await client.post(url, headers=_auth(tokens), json={})
    assert none.status_code == 400 and none.json()["detail"]["code"] == "no_crm_connected"
    unconnected = await client.post(url, headers=_auth(tokens), json={"provider": "zoho"})
    assert unconnected.status_code == 404

    await seed_connection(db_session, tid, "salesforce", extra={"instance_url": "https://acme.my.salesforce.com"})
    await seed_connection(db_session, tid, "zoho", extra={"api_domain": "https://www.zohoapis.in"})
    sf = "https://acme.my.salesforce.com/services/data/v60.0"
    providers.add("GET", sf + "/query", {"records": []})
    providers.add("POST", sf + "/sobjects/Lead", {"id": "00Q1", "success": True}, status=201)
    providers.add(
        "POST", "https://www.zohoapis.in/crm/v2/Leads/upsert",
        {"data": [{"status": "success", "action": "insert"}]},
    )
    out = await client.post(url, headers=_auth(tokens), json={})
    assert out.status_code == 200, out.text
    assert {r["provider"]: r["ok"] for r in out.json()} == {"salesforce": True, "zoho": True}
    created = json.loads(providers.calls_to("POST", sf + "/sobjects/Lead")[0].content)
    assert created["LastName"] == "Hopper" and created["FirstName"] == "Grace" and created["Email"] == "grace@navy.test"
    assert created["LeadSource"] == "AutoNXT"
    query = providers.calls_to("GET", sf + "/query")[0].url.params["q"]
    assert "grace@navy.test" in query

    # second push finds the existing Salesforce lead and PATCHes instead of duplicating
    providers.routes = [r for r in providers.routes if r[1] != sf + "/query"]
    providers.add("GET", sf + "/query", {"records": [{"Id": "00Q1"}]})
    providers.add("PATCH", sf + "/sobjects/Lead/00Q1", {}, status=204)
    again = await client.post(url, headers=_auth(tokens), json={"provider": "salesforce"})
    assert again.json() == [{"provider": "salesforce", "ok": True, "detail": "Salesforce lead updated"}]
    assert "Company" not in json.loads(providers.calls_to("PATCH", sf + "/sobjects/Lead/00Q1")[0].content)

    # another tenant cannot push this lead
    other = await _signup(client, slug="push-other", email="o@pushother.com")
    assert (await client.post(url, headers=_auth(other), json={})).status_code == 404


async def test_ai_tool_created_leads_are_pushed_best_effort(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    from app.tools import crm_tools
    from app.tools.context import ToolContext

    tenant_id, customer_id, _ = await make_tenant(db_session)
    await seed_connection(db_session, tenant_id, "hubspot")
    providers.add("POST", "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert", {}, status=500)
    ctx = ToolContext(db=db_session, tenant_id=tenant_id, agent_id=None, call_id=None,
                      conversation_id=None, customer_id=customer_id)
    result = await crm_tools.create_lead(ctx, name="Walk In", email="walk@in.test")
    assert result["created"] is True and providers.calls  # attempted, failure swallowed


# ---------------------------------------------------------------- Slack via connector dispatch


async def test_dispatch_event_posts_to_selected_slack_channel(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    from app.services.platform_connectors import dispatch_event

    tenant_id, _, _ = await make_tenant(db_session)
    await seed_connection(
        db_session, tenant_id, "slack", expires_in=None, refresh=None,
        extra={"selected": {"channel_id": "C1", "channel_name": "#support", "events": ["ticket.created"]}},
    )
    providers.add("POST", "https://slack.com/api/chat.postMessage", {"ok": True, "ts": "1"})
    results = await dispatch_event(db_session, tenant_id, "ticket.created", {"subject": "Printer on fire", "priority": "high"})
    assert [r["ok"] for r in results] == [True]
    sent = providers.calls[0]
    payload = json.loads(sent.content)
    assert payload["channel"] == "C1" and "Printer on fire" in payload["text"] and "New ticket" in payload["text"]
    assert sent.headers["Authorization"] == "Bearer a-1"

    # events the tenant did not subscribe to are not posted
    providers.calls.clear()
    assert await dispatch_event(db_session, tenant_id, "call.ended", {"call_id": "1"}) == []
    assert providers.calls == []

    # a revoked token flips the connection to revoked
    providers.routes.clear()
    providers.add("POST", "https://slack.com/api/chat.postMessage", {"ok": False, "error": "token_revoked"})
    out = await dispatch_event(db_session, tenant_id, "ticket.created", {"subject": "x"})
    assert out[0]["ok"] is False and "token_revoked" in out[0]["detail"]
    await db_session.commit()
    conn = await get_connection(db_session, tenant_id, "slack")
    await db_session.refresh(conn)
    assert conn.status == "revoked"


async def test_dispatch_without_slack_connection_is_unchanged(db_session: AsyncSession, providers) -> None:
    from app.services.platform_connectors import dispatch_event

    tenant_id, _, _ = await make_tenant(db_session)
    assert await dispatch_event(db_session, tenant_id, "ticket.created", {}) == []
    assert providers.calls == []


# ---------------------------------------------------------------- WhatsApp Embedded Signup


async def test_whatsapp_embedded_signup_creates_account(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession, monkeypatch
) -> None:
    g = "https://graph.facebook.com/v21.0"
    monkeypatch.setattr(creds, "meta_whatsapp_config_id", "wa-cfg")
    tokens = await _signup(client, slug="wa-co", email="o@waco.com")
    other = await _signup(client, slug="wa-other", email="o@waother.com")
    cfg = (await client.get("/api/v1/oauth/whatsapp/config", headers=_auth(tokens))).json()
    assert cfg == {"configured": True, "app_id": "metaid", "config_id": "wa-cfg", "graph_api_version": "v21.0"}

    providers.add("GET", g + "/oauth/access_token", {"access_token": "biz-token"})
    providers.add("GET", g + "/PN1", {"display_phone_number": "+91 98765 43210", "verified_name": "Acme"})
    providers.add("POST", g + "/WABA1/subscribed_apps", {"success": True})
    providers.add("POST", g + "/PN1/register", {"success": True})
    body = {"code": "signup-code", "phone_number_id": "PN1", "waba_id": "WABA1"}
    resp = await client.post("/api/v1/oauth/whatsapp/embedded-signup", headers=_auth(tokens), json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["phone_number_id"] == "PN1" and data["registration_pin"] and len(data["registration_pin"]) == 6
    row = (await db_session.execute(select(WhatsAppAccount))).scalar_one()
    assert decrypt_secret(row.encrypted_access_token) == "biz-token"
    assert row.display_phone_number == "+91 98765 43210"
    assert providers.calls_to("GET", g + "/oauth/access_token")[0].url.params["code"] == "signup-code"
    # repeating is idempotent for the same tenant; another tenant cannot take the number
    assert (await client.post("/api/v1/oauth/whatsapp/embedded-signup", headers=_auth(tokens), json=body)).status_code == 200
    clash = await client.post("/api/v1/oauth/whatsapp/embedded-signup", headers=_auth(other), json=body)
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "account_in_use"
    bad = await client.post(
        "/api/v1/oauth/whatsapp/embedded-signup", headers=_auth(tokens), json={**body, "pin": "12"}
    )
    assert bad.status_code == 422


async def test_whatsapp_signup_not_configured(client: AsyncClient, monkeypatch) -> None:
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "meta_app_id", "")
    tokens = await _signup(client, slug="wa2-co", email="o@wa2co.com")
    resp = await client.post(
        "/api/v1/oauth/whatsapp/embedded-signup", headers=_auth(tokens),
        json={"code": "c", "phone_number_id": "P", "waba_id": "W"},
    )
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "provider_not_configured"


# ---------------------------------------------------------------- catalog


async def test_connector_catalog_marks_oauth_providers_supported(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="cat-co", email="o@catco.com")
    catalog = {c["type"]: c for c in (await client.get("/api/v1/platform/connectors/catalog", headers=_auth(tokens))).json()}
    for typ, provider in (("google_calendar", "google"), ("outlook", "microsoft"), ("salesforce", "salesforce"),
                          ("slack", "slack"), ("hubspot", "hubspot"), ("zoho", "zoho"), ("shopify", "shopify")):
        assert catalog[typ]["supported"] is True and catalog[typ]["oauth_provider"] == provider
    assert catalog["zapier"]["oauth_provider"] is None
