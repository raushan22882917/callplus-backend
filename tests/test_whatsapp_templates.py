"""WhatsApp templates: write with named blanks, Meta's rules, submit, approval
webhook, sync, and sending with blanks filled (inbox + booking reminders)."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import whatsapp_templates as tpl
from packages.database.models import Message, WhatsAppTemplate


def _template(**kw) -> WhatsAppTemplate:
    base = {"name": "booking_reminder", "language": "en", "category": "UTILITY", "body": "Hi {{name}}, see you on {{date}} at {{time}}. Thanks!", "buttons": [], "examples": {}}
    return WhatsAppTemplate(**(base | kw))


def test_named_blanks_become_numbered_and_repeat_reuses_the_number():
    text, keys = tpl.compile_body("Hi {{name}}, {{ date }} works. Bye {{name}}!")
    assert text == "Hi {{1}}, {{2}} works. Bye {{1}}!" and keys == ["name", "date"]


def test_every_starter_template_passes_metas_rules():
    for item in tpl.LIBRARY:
        t = _template(**{k: item[k] for k in ("name", "category", "body", "header_text", "footer_text", "buttons")})
        assert tpl.validate(t) == [], (item["name"], tpl.validate(t))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"body": "{{name}} your booking is confirmed for tomorrow."}, "very start or end"),
        ({"body": "Your booking is confirmed for {{date}}"}, "very start or end"),
        ({"body": "Hi there, {{date}}{{time}} works for us."}, "between two blanks"),
        ({"body": "Hi {{1}}, thanks for booking with us today."}, "named blanks"),
        ({"name": "Booking Reminder!"}, "lowercase letters"),
        ({"buttons": [{"type": "URL", "text": "Pay", "url": "http://x.com"}]}, "https://"),
        ({"category": "AUTHENTICATION"}, "exactly one blank"),
    ],
)
def test_rule_breaks_are_explained_in_plain_words(changes, message):
    assert any(message in p for p in tpl.validate(_template(**changes)))


def test_payload_preview_and_parameters():
    t = _template(header_text="Reminder", footer_text="Reply STOP to opt out", buttons=[{"type": "QUICK_REPLY", "text": "Reschedule"}], examples={"name": "Asha"})
    payload = tpl.meta_payload(t)
    body = next(c for c in payload["components"] if c["type"] == "BODY")
    assert body["text"] == "Hi {{1}}, see you on {{2}} at {{3}}. Thanks!"
    assert body["example"]["body_text"] == [["Asha", "Saturday 4 October", "2:00 pm"]]
    assert [c["type"] for c in payload["components"]] == ["HEADER", "BODY", "FOOTER", "BUTTONS"]
    t.variables = ["name", "date", "time"]
    assert [p["text"] for p in tpl.body_parameters(t, {"name": "Roshan", "date": "Sunday"})] == ["Roshan", "Sunday", "-"]
    assert tpl.render(t, {"name": "Roshan"}).startswith("Reminder\nHi Roshan, see you on Saturday 4 October")


def test_whatsapp_24_hour_window():
    now = datetime.now(UTC)
    assert tpl.window_open(now - timedelta(hours=23), now) and not tpl.window_open(now - timedelta(hours=25), now)
    assert not tpl.window_open(None, now)


# ------------------------------------------------------------------ API with a fake Meta


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"requests": [], "templates": [], "sent": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        path = request.url.path
        if path.endswith("/message_templates") and request.method == "POST":
            body = json.loads(request.content)
            state["templates"].append(body)
            return httpx.Response(200, json={"id": "tmpl-1", "status": "PENDING", "category": body["category"]})
        if path.endswith("/message_templates") and request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": "tmpl-1", "name": "booking_reminder", "language": "en", "status": "APPROVED", "category": "UTILITY"},
                {"id": "tmpl-9", "name": "made_in_meta", "language": "hi", "status": "APPROVED", "category": "MARKETING",
                 "components": [{"type": "BODY", "text": "Namaste {{1}}, offer for {{2}} this week only!"}]},
            ]})
        if path.endswith("/message_templates") and request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        if path.endswith("/messages"):
            state["sent"].append(json.loads(request.content))
            return httpx.Response(200, json={"messages": [{"id": f"wamid.t{len(state['sent'])}"}]})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(tpl.httpx, "AsyncClient", lambda *a, **k: real(transport=transport))
    return state


async def _owner(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "Apna Salon", "tenant_slug": slug, "full_name": "O", "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    auth = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    acct = await client.post(
        "/api/v1/whatsapp-accounts", headers=auth,
        json={"phone_number_id": "555", "waba_id": "waba-1", "display_phone_number": "+15550", "access_token": "t"},
    )
    assert acct.status_code == 201
    return auth


async def test_create_from_library_submit_get_approved_and_send_from_inbox(
    client: AsyncClient, db_session: AsyncSession, meta: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _owner(client, "tmpl-co")
    library = (await client.get("/api/v1/whatsapp-templates/library", headers=auth)).json()
    starter = next(t for t in library["library"] if t["purpose"] == "booking_reminder")
    assert "name" in library["variables"]

    created = await client.post(
        "/api/v1/whatsapp-templates", headers=auth,
        json={k: starter[k] for k in ("name", "category", "purpose", "header_text", "body", "footer_text", "buttons")} | {"submit": True},
    )
    assert created.status_code == 201, created.text
    t = created.json()
    assert t["status"] == "PENDING" and t["variables"] == ["name", "service", "business", "date", "time"]
    sent_to_meta = meta["templates"][0]
    assert "{{1}}" in next(c for c in sent_to_meta["components"] if c["type"] == "BODY")["text"]

    # A second one with the same name is refused in plain words.
    dup = await client.post("/api/v1/whatsapp-templates", headers=auth, json={"name": starter["name"], "body": "Hi {{name}}, thanks a lot!"})
    assert dup.status_code == 409 and "already have a template" in dup.json()["detail"]

    # Meta approves it through the webhook.
    body = json.dumps({"entry": [{"changes": [{"field": "message_template_status_update", "value": {
        "event": "APPROVED", "message_template_id": "tmpl-1", "message_template_name": "booking_reminder", "reason": "NONE"}}]}]}).encode()
    hook = await client.post(
        "/api/v1/webhooks/whatsapp/meta", content=body,
        headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"},
    )
    assert hook.status_code == 200
    approved = (await client.get("/api/v1/whatsapp-templates", headers=auth)).json()[0]
    assert approved["status"] == "APPROVED"
    edit = await client.put(f"/api/v1/whatsapp-templates/{approved['id']}", headers=auth, json={"body": "Hi {{name}}, changed text here."})
    assert edit.status_code == 409  # approved templates can't be edited in place

    # Re-open an old chat from the inbox with the template; blanks come from the customer.
    async def noop(*a, **k):
        return None

    async def first(mid):
        return True

    async def fake_llm(**kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "Hi!"}}]}

    async def fake_send(account, *, to, body):
        return {"messages": [{"id": "x"}]}

    monkeypatch.setattr("app.services.whatsapp_inbound.mark_read", noop)
    monkeypatch.setattr("app.services.whatsapp_inbound.claim", first)
    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_llm)
    monkeypatch.setattr("app.services.channels.send_text_message", fake_send)
    inbound = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "555"},
        "contacts": [{"wa_id": "919811122233", "profile": {"name": "Roshan Kumar"}}],
        "messages": [{"id": "in-1", "from": "919811122233", "type": "text", "text": {"body": "hi"}}]}}]}]}).encode()
    await client.post(
        "/api/v1/webhooks/whatsapp/meta", content=inbound,
        headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"", inbound, hashlib.sha256).hexdigest(), "Content-Type": "application/json"},
    )
    conversation_id = (await client.get("/api/v1/channels/inbox", headers=auth)).json()[0]["id"]
    thread = (await client.get(f"/api/v1/channels/conversations/{conversation_id}", headers=auth)).json()
    assert thread["whatsapp_window_open"] is True

    sent = await client.post(
        f"/api/v1/channels/conversations/{conversation_id}/template", headers=auth,
        json={"template_id": approved["id"], "values": {"service": "Haircut", "date": "Sunday", "time": "11 am"}},
    )
    assert sent.status_code == 201, sent.text
    params = [p["text"] for p in meta["sent"][-1]["template"]["components"][0]["parameters"]]
    assert params == ["Roshan", "Haircut", "Apna Salon", "Sunday", "11 am"]
    assert meta["sent"][-1]["to"] == "919811122233"
    stored = (await db_session.execute(select(Message).where(Message.provider_message_id == "wamid.t1"))).scalar_one()
    assert stored.text.startswith("Reminder\nHi Roshan, a reminder of your Haircut at Apna Salon")


async def test_sync_brings_in_status_and_templates_made_in_meta(client: AsyncClient, meta: dict) -> None:
    auth = await _owner(client, "tmpl-sync")
    await client.post("/api/v1/whatsapp-templates", headers=auth, json={"name": "booking_reminder", "body": "Hi {{name}}, see you on {{date}} soon.", "submit": True})
    synced = (await client.post("/api/v1/whatsapp-templates/sync", headers=auth)).json()
    by_name = {t["name"]: t for t in synced}
    assert by_name["booking_reminder"]["status"] == "APPROVED"
    imported = by_name["made_in_meta"]
    assert imported["body"] == "Namaste {{var1}}, offer for {{var2}} this week only!" and imported["variables"] == ["var1", "var2"]

    gone = await client.delete(f"/api/v1/whatsapp-templates/{imported['id']}", headers=auth)
    assert gone.status_code == 204
    assert any(r.method == "DELETE" for r in meta["requests"])


async def test_submit_refuses_a_template_that_breaks_the_rules(client: AsyncClient, meta: dict) -> None:
    auth = await _owner(client, "tmpl-rules")
    bad = await client.post("/api/v1/whatsapp-templates", headers=auth, json={"name": "x", "body": "{{name}} hello there friend", "submit": True})
    assert bad.status_code == 422 and "very start or end" in bad.json()["detail"] and "Saved as a draft" in bad.json()["detail"]
    assert meta["templates"] == []  # nothing reached Meta
    kept = (await client.get("/api/v1/whatsapp-templates", headers=auth)).json()
    assert [t["status"] for t in kept] == ["DRAFT"]  # the owner's work is kept


async def test_booking_reminder_uses_the_approved_template(db_session: AsyncSession, meta: dict) -> None:
    from app.core.crypto import encrypt_secret
    from app.services import operations as ops

    from packages.database.models import Customer, Tenant, WhatsAppAccount
    from packages.database.models.operations import Appointment, AppointmentResource, BookableService

    tenant = Tenant(name="Apna Salon", slug="reminder-salon")
    db_session.add(tenant)
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, name="Roshan Kumar", phone="+919811122233")
    account = WhatsAppAccount(tenant_id=tenant.id, phone_number_id="555", waba_id="w", display_phone_number="+1", encrypted_access_token=encrypt_secret("t"))
    service = BookableService(tenant_id=tenant.id, name="Haircut", duration_minutes=30, price=300)
    resource = AppointmentResource(tenant_id=tenant.id, name="Asha", timezone="Asia/Kolkata", working_hours={})
    db_session.add_all([customer, account, service, resource])
    await db_session.flush()
    item = next(t for t in tpl.LIBRARY if t["purpose"] == "booking_reminder")
    db_session.add(WhatsAppTemplate(
        tenant_id=tenant.id, whatsapp_account_id=account.id, name=item["name"], language="en", category="UTILITY",
        purpose="booking_reminder", body=item["body"], buttons=[], examples={}, status="APPROVED",
        variables=tpl.compile_body(item["body"])[1],
    ))
    soon = datetime.now(UTC) + timedelta(hours=3)
    appt = Appointment(tenant_id=tenant.id, customer_id=customer.id, service_id=service.id, resource_id=resource.id, start_at=soon, end_at=soon + timedelta(minutes=30))
    db_session.add(appt)
    await db_session.commit()

    result = await ops.send_due_reminders(db_session, tenant_id=tenant.id)
    assert (result.sent, result.failed) == (1, 0)
    sent = meta["sent"][-1]["template"]
    assert sent["name"] == "booking_reminder"
    params = [p["text"] for p in sent["components"][0]["parameters"]]
    assert params[:3] == ["Roshan", "Haircut", "Apna Salon"]
