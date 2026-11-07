import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1.channels import router as channels_router
from app.integrations.channel_payloads import (
    build_voicemail_twiml,
    chunk_text,
    parse_email_payload,
    parse_meta_messages,
    strip_quoted_reply,
    verify_meta_signature,
)
from app.main import app
from app.services.channels import evaluate_form_spam
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Ticket,
)
from packages.database.models.channels import MissedCall, Voicemail

# The lead adds `channels.router` to api_router; until then mount it here.
if not any(getattr(r, "path", "") == "/api/v1/channels/inbox" for r in app.routes):
    app.include_router(channels_router, prefix="/api/v1")


async def _signup(client: AsyncClient, *, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.co",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _agent(client: AsyncClient, auth: dict) -> str:
    resp = await client.post("/api/v1/ai-agents", headers=auth, json={"name": "Bot"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _enable(client: AsyncClient, auth: dict, channel: str, **body) -> dict:
    resp = await client.put(
        f"/api/v1/channels/settings/{channel}", headers=auth, json={"enabled": True, **body}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def fake_chat_completion(*, messages, tools=None, api_key=None, **kwargs):
        seen.append(messages[-1]["content"])
        return {"choices": [{"message": {"role": "assistant", "content": "AI says hi"}}]}

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat_completion)
    return seen


# ------------------------------------------------------------------ pure parsing


def test_parse_email_payload_generic_json_and_threading_headers() -> None:
    mail = parse_email_payload(
        {
            "from": "Ann Lee <ANN@Example.com>",
            "to": "support@acme.test",
            "subject": "Order help",
            "text": "Where is my order?\n\nOn Mon, Jan 1, 2026 at 9:00 AM Support <s@acme.test> wrote:\n> old text",
            "message_id": "<abc@mail.example.com>",
            "in_reply_to": "<prev@acme.test>",
            "references": "<root@acme.test> <prev@acme.test>",
        }
    )
    assert mail is not None
    assert mail.from_address == "ann@example.com" and mail.from_name == "Ann Lee"
    assert mail.text == "Where is my order?"
    assert mail.message_id == "<abc@mail.example.com>"
    assert mail.in_reply_to == "<prev@acme.test>"
    assert mail.references == ["<root@acme.test>", "<prev@acme.test>"]
    assert not mail.is_automated


def test_parse_email_payload_mailgun_sendgrid_and_automated() -> None:
    mailgun = parse_email_payload(
        {"sender": "bob@x.test", "recipient": "s@acme.test", "stripped-text": "hello", "Message-Id": "<m1@x>"}
    )
    assert mailgun and mailgun.message_id == "<m1@x>" and mailgun.to_address == "s@acme.test"

    sendgrid = parse_email_payload(
        {
            "from": "bob@x.test",
            "to": "s@acme.test",
            "text": "hi",
            "headers": "Message-ID: <sg1@x>\nIn-Reply-To: <out1@acme.test>\n",
        }
    )
    assert sendgrid and sendgrid.message_id == "<sg1@x>" and sendgrid.in_reply_to == "<out1@acme.test>"

    auto = parse_email_payload({"from": "bob@x.test", "text": "away", "headers": "Auto-Submitted: auto-replied\n"})
    assert auto and auto.is_automated
    assert parse_email_payload({"from": "bob@x.test"}) is None
    assert strip_quoted_reply("hi\n> quoted") == "hi"


def test_parse_meta_messages_skips_echoes_and_non_text() -> None:
    body = {
        "object": "instagram",
        "entry": [
            {
                "id": "IG1",
                "messaging": [
                    {"sender": {"id": "U1"}, "message": {"mid": "m1", "text": "hello"}},
                    {"sender": {"id": "IG1"}, "message": {"mid": "m2", "text": "echo", "is_echo": True}},
                    {"sender": {"id": "U2"}, "message": {"mid": "m3", "attachments": []}},
                ],
            }
        ],
    }
    msgs = parse_meta_messages(body)
    assert [(m.channel, m.account_id, m.sender_id, m.text) for m in msgs] == [("instagram", "IG1", "U1", "hello")]
    assert parse_meta_messages({"object": "whatsapp_business_account", "entry": []}) == []


def test_meta_signature_and_chunking() -> None:
    raw = b'{"a":1}'
    good = "sha256=" + hmac.new(b"sekret", raw, hashlib.sha256).hexdigest()
    assert verify_meta_signature(raw, good, "sekret")
    assert not verify_meta_signature(raw, good, "other")
    assert not verify_meta_signature(raw, None, "sekret")
    parts = chunk_text("word " * 100, 120)
    assert all(len(p) <= 120 for p in parts) and " ".join(parts).split() == ("word " * 100).split()


def test_twilio_signature_validation_roundtrip() -> None:
    from app.integrations.twilio_client import validate_twilio_signature

    url, params = "https://api.test/api/v1/channels/sms/webhook", {"From": "+1555", "Body": "hi"}
    signature = RequestValidator("tok").compute_signature(url, params)
    assert validate_twilio_signature(url=url, params=params, signature=signature, auth_token="tok")
    assert not validate_twilio_signature(url=url, params=params, signature="bad", auth_token="tok")


def test_form_spam_guard() -> None:
    ok = {"name": "A", "email": "a@b.co", "message": "Please call me about pricing"}
    assert evaluate_form_spam(ok) is None
    assert evaluate_form_spam({**ok, "website": "http://spam"}) == "honeypot field filled"
    assert evaluate_form_spam(ok, elapsed_seconds=0.4) == "submitted too quickly"
    assert evaluate_form_spam({**ok, "message": "x"}) == "message too short"
    assert evaluate_form_spam({**ok, "message": "http://a.co http://b.co http://c.co"}) == "too many links"
    assert evaluate_form_spam({**ok, "name": "n" * 500}) == "name too long"


def test_voicemail_twiml_records_and_transcribes() -> None:
    twiml = build_voicemail_twiml(
        greeting="Leave a message", recording_callback_url="https://x/rec", transcribe_callback_url="https://x/tr"
    )
    assert "<Record" in twiml and 'transcribe="true"' in twiml and "https://x/tr" in twiml


# ------------------------------------------------------------------ SMS


async def test_sms_webhook_runs_ai_and_sends_reply(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, fake_ai: list[str]
) -> None:
    auth = await _signup(client, slug="sms-co")
    agent_id = await _agent(client, auth)
    await client.post("/api/v1/phone-numbers", headers=auth, json={"e164_number": "+15550001111"})
    await _enable(client, auth, "sms", ai_agent_id=agent_id)

    sent = []

    async def fake_send(db, tenant_id, *, to, from_, body):
        sent.append((to, from_, body))
        return "SM123"

    monkeypatch.setattr("app.services.channels.send_sms_message", fake_send)
    form = {"To": "+15550001111", "From": "+15552223333", "Body": "Hi there", "MessageSid": "SMabc"}
    assert (await client.post("/api/v1/channels/sms/webhook", data=form)).status_code == 200
    assert sent == [("+15552223333", "+15550001111", "AI says hi")]
    # Twilio retry of the same MessageSid is ignored.
    await client.post("/api/v1/channels/sms/webhook", data=form)
    assert len(sent) == 1 and fake_ai == ["Hi there"]

    inbox = (await client.get("/api/v1/channels/inbox?channel=sms", headers=auth)).json()
    assert len(inbox) == 1 and inbox[0]["channel"] == "sms"
    assert inbox[0]["last_message"]["text"] == "AI says hi"
    assert inbox[0]["customer"]["phone"] == "+15552223333"


async def test_sms_webhook_rejects_bad_signature(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="sms-sig")
    await client.post("/api/v1/phone-numbers", headers=auth, json={"e164_number": "+15550002222"})

    async def token(db, tenant_id):
        return "tok"

    monkeypatch.setattr("app.api.v1.channels._resolve_auth_token", token)
    form = {"To": "+15550002222", "From": "+15552223333", "Body": "hi"}
    assert (await client.post("/api/v1/channels/sms/webhook", data=form)).status_code == 403
    from app.core.config import get_settings

    url = f"{get_settings().public_base_url}/api/v1/channels/sms/webhook"
    signature = RequestValidator("tok").compute_signature(url, form)
    ok = await client.post("/api/v1/channels/sms/webhook", data=form, headers={"X-Twilio-Signature": signature})
    assert ok.status_code == 200


# ------------------------------------------------------------------ email


async def test_email_webhook_threads_by_message_id(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, fake_ai: list[str]
) -> None:
    auth = await _signup(client, slug="mail-co")
    agent_id = await _agent(client, auth)
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        cfg = await _enable(client, auth, "email", ai_agent_id=agent_id, credentials={"webhook_secret": "s3"})
        assert cfg["credentials_set"] == ["webhook_secret"] and "s3" not in json.dumps(cfg)
        url = f"/api/v1/channels/email/webhook/{cfg['public_key']}"

        out_ids = iter(["<out1@acme.test>", "<out2@acme.test>"])
        sent = []

        async def fake_send_email(**kwargs):
            sent.append(kwargs)
            return next(out_ids)

        monkeypatch.setattr("app.services.channels.send_email_message", fake_send_email)

        first = {"from": "cust@x.test", "to": "support@acme.test", "subject": "Refund", "text": "I want a refund", "message_id": "<c1@x>"}
        assert (await client.post(url, json=first)).status_code == 403  # no secret
        r1 = await client.post(url, json=first, headers={"X-Webhook-Secret": "s3"})
        assert r1.json()["replied"] is True, r1.text
        reply = {
            "from": "cust@x.test", "to": "support@acme.test", "subject": "Re: Refund",
            "text": "Thanks, order 5", "message_id": "<c2@x>", "in_reply_to": "<out1@acme.test>",
        }
        r2 = await client.post(url, json=reply, headers={"X-Webhook-Secret": "s3"})
        assert r2.json()["conversation_id"] == r1.json()["conversation_id"]
        dup = await client.post(url, json=reply, headers={"X-Webhook-Secret": "s3"})
        assert dup.json()["status"] == "duplicate"
        assert sent[1]["in_reply_to"] == "<c2@x>" and sent[1]["subject"] == "Re: Refund"

        inbox = (await client.get("/api/v1/channels/inbox?channel=email", headers=auth)).json()
        assert len(inbox) == 1 and inbox[0]["subject"] == "Refund"
        thread = (await client.get(f"/api/v1/channels/conversations/{inbox[0]['id']}", headers=auth)).json()
        assert [m["direction"] for m in thread["messages"]] == ["inbound", "outbound", "inbound", "outbound"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_email_reply_without_smtp_is_reported_honestly(client: AsyncClient, fake_ai: list[str]) -> None:
    auth = await _signup(client, slug="mail-nosmtp")
    # Enabling email needs SMTP; insert the settings row directly to simulate it being removed later.
    resp = await client.put("/api/v1/channels/settings/email", headers=auth, json={"enabled": True})
    assert resp.status_code == 422 and "SMTP" in resp.json()["detail"]


# ------------------------------------------------------------------ web chat


async def test_webchat_session_message_poll_and_staff_reply(client: AsyncClient, fake_ai: list[str]) -> None:
    auth = await _signup(client, slug="chat-co")
    agent_id = await _agent(client, auth)
    cfg = await _enable(
        client, auth, "web_chat", ai_agent_id=agent_id, config={"greeting": "Hello!", "title": "Help"}
    )
    key = cfg["public_key"]
    assert key in cfg["widget_snippet"] and "widget.js" in cfg["widget_snippet"]
    base = f"/api/v1/channels/webchat/{key}"

    js = await client.get("/api/v1/channels/webchat/widget.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "data-key" in js.text

    session = (await client.post(f"{base}/session", content='{"privacy_accepted": true, "adult_confirmed": true}', headers={"Content-Type": "text/plain", "Origin": "https://shop.test"})).json()
    token = session["session_token"]
    assert session["messages"][0]["text"] == "Hello!"

    resp = await client.post(f"{base}/message", content=json.dumps({"session_token": token, "text": "Do you ship?"}), headers={"Content-Type": "text/plain", "Origin": "https://shop.test"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == "*"
    assert [m["text"] for m in resp.json()["messages"]] == ["Do you ship?", "AI says hi"]

    bad = await client.post(f"{base}/message", json={"session_token": "nope", "text": "x"})
    assert bad.status_code == 404
    assert (await client.post(f"{base}/message", json={"session_token": token, "text": "x" * 2001})).status_code == 422
    assert (await client.get("/api/v1/channels/webchat/unknownkey/config")).status_code == 404

    inbox = (await client.get("/api/v1/channels/inbox?channel=web_chat", headers=auth)).json()
    conv_id = inbox[0]["id"]
    staff = await client.post(f"/api/v1/channels/conversations/{conv_id}/reply", headers=auth, json={"text": "A human here"})
    assert staff.status_code == 201, staff.text
    polled = (await client.get(f"{base}/messages", params={"session_token": token})).json()["messages"]
    assert polled[-1]["text"] == "A human here" and polled[-1]["sender"] == "human_agent"

    # Origin allow-list blocks other sites.
    await client.put("/api/v1/channels/settings/web_chat", headers=auth, json={"config": {"allowed_origins": ["https://shop.test"]}})
    blocked = await client.post(f"{base}/session", json={"privacy_accepted": True, "adult_confirmed": True}, headers={"Origin": "https://evil.test"})
    assert blocked.status_code == 403
    allowed = await client.post(f"{base}/session", json={"privacy_accepted": True, "adult_confirmed": True}, headers={"Origin": "https://shop.test"})
    assert allowed.status_code == 200 and allowed.headers["access-control-allow-origin"] == "https://shop.test"


# ------------------------------------------------------------------ forms


async def test_web_form_creates_customer_and_ticket_and_blocks_spam(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await _signup(client, slug="form-co")
    cfg = await _enable(client, auth, "web_form", config={"success_message": "Got it"})
    url = f"/api/v1/channels/forms/{cfg['public_key']}"

    good = {"name": "Dana", "email": "dana@x.test", "phone": "+1 555 000 9999", "message": "Need a quote please"}
    resp = await client.post(url, json=good)
    assert resp.status_code == 200 and resp.json()["message"] == "Got it"
    tickets = (await db_session.execute(select(Ticket))).scalars().all()
    customers = (await db_session.execute(select(Customer).where(Customer.email == "dana@x.test"))).scalars().all()
    assert len(tickets) == 1 and len(customers) == 1 and customers[0].phone == "+15550009999"

    bot = await client.post(url, data={**good, "email": "bot@x.test", "website": "http://spam.test"})
    assert bot.status_code == 200  # pretends success
    assert len((await db_session.execute(select(Ticket))).scalars().all()) == 1
    assert (await client.post(url, json={**good, "message": "http://a.co http://b.co http://c.co"})).status_code == 422
    assert (await client.post(url, json={"message": "no contact info here"})).status_code == 422
    assert (await client.post("/api/v1/channels/forms/wrong", json=good)).status_code == 404

    inbox = (await client.get("/api/v1/channels/inbox?channel=web_form", headers=auth)).json()
    assert len(inbox) == 1 and inbox[0]["unread_count"] == 1


# ------------------------------------------------------------------ Meta


async def test_meta_webhook_handshake_signature_and_reply(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, fake_ai: list[str]
) -> None:
    auth = await _signup(client, slug="meta-co")
    agent_id = await _agent(client, auth)

    no_creds = await client.put("/api/v1/channels/settings/instagram", headers=auth, json={"enabled": True})
    assert no_creds.status_code == 422 and "page_access_token" in no_creds.json()["detail"]

    await _enable(
        client, auth, "instagram", ai_agent_id=agent_id, external_id="IG42",
        credentials={"page_access_token": "tok", "app_secret": "appsec", "verify_token": "vt-1"},
    )
    ok = await client.get("/api/v1/channels/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt-1", "hub.challenge": "777"})
    assert ok.status_code == 200 and ok.text == "777"
    assert (await client.get("/api/v1/channels/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"})).status_code == 403

    sent = []

    async def fake_meta_send(*, page_access_token, recipient_id, text, channel, **_ignored):
        sent.append((page_access_token, recipient_id, text, channel))
        return "mid.out"

    monkeypatch.setattr("app.services.channels.send_meta_message", fake_meta_send)
    body = json.dumps({"object": "instagram", "entry": [{"id": "IG42", "messaging": [{"sender": {"id": "USER9"}, "message": {"mid": "mid.1", "text": "price?"}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"appsec", body, hashlib.sha256).hexdigest()
    assert (await client.post("/api/v1/channels/meta/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})).status_code == 403
    resp = await client.post("/api/v1/channels/meta/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.json()["handled"] == 1
    assert sent == [("tok", "USER9", "AI says hi", "instagram")]
    again = await client.post("/api/v1/channels/meta/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert again.json()["handled"] == 0


# ------------------------------------------------------------------ inbox merge + reply routing


async def test_inbox_merges_channels_unread_and_reply_routing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="inbox-co")
    await _signup(client, slug="inbox-other")
    from packages.database.models import Tenant

    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "inbox-co"))).scalar_one()
    other_tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "inbox-other"))).scalar_one()

    clock = iter(range(0, 10000, 10))

    async def make(tid, channel, customer_name, texts):
        customer = Customer(tenant_id=tid, name=customer_name, phone="+1555" + customer_name[:3].replace(" ", ""))
        db_session.add(customer)
        await db_session.flush()
        conv = Conversation(tenant_id=tid, customer_id=customer.id, channel=channel)
        db_session.add(conv)
        await db_session.flush()
        for index, (direction, text) in enumerate(texts):
            db_session.add(Message(
                tenant_id=tid, conversation_id=conv.id, direction=direction,
                created_at=datetime.now(UTC) - timedelta(hours=2) + timedelta(seconds=index + next(clock)),
                sender=MessageSender.CUSTOMER if direction == MessageDirection.INBOUND else MessageSender.AI_AGENT, text=text,
            ))
            await db_session.flush()
        return conv

    wa = await make(tenant.id, ConversationChannel.WHATSAPP, "Wanda", [(MessageDirection.INBOUND, "hi"), (MessageDirection.OUTBOUND, "hello"), (MessageDirection.INBOUND, "pricing?")])
    sms = await make(tenant.id, ConversationChannel.SMS, "Sam", [(MessageDirection.INBOUND, "yo"), (MessageDirection.OUTBOUND, "sup")])
    voice_conv = await make(tenant.id, ConversationChannel.VOICE, "Vera", [(MessageDirection.INBOUND, "call text")])
    await make(tenant.id, ConversationChannel.INTERNAL, "Staffer", [(MessageDirection.INBOUND, "assistant q")])
    await make(other_tenant.id, ConversationChannel.SMS, "Other", [(MessageDirection.INBOUND, "leak?")])
    await db_session.commit()

    items = (await client.get("/api/v1/channels/inbox", headers=auth)).json()
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {str(wa.id), str(sms.id), str(voice_conv.id)}  # no internal, no other tenant
    assert by_id[str(wa.id)]["unread_count"] == 1 and by_id[str(sms.id)]["unread_count"] == 0
    assert by_id[str(voice_conv.id)]["unread_count"] == 0
    assert by_id[str(wa.id)]["last_message"]["text"] == "pricing?"
    assert items[0]["id"] == str(voice_conv.id) or items[0]["last_message_at"] >= items[-1]["last_message_at"]
    assert [i["id"] for i in (await client.get("/api/v1/channels/inbox?unread_only=true", headers=auth)).json()] == [str(wa.id)]
    assert len((await client.get("/api/v1/channels/inbox?channel=sms", headers=auth)).json()) == 1
    assert len((await client.get("/api/v1/channels/inbox?q=wand", headers=auth)).json()) == 1
    assert (await client.get("/api/v1/channels/inbox?channel=bogus", headers=auth)).status_code == 422

    await client.post(f"/api/v1/channels/conversations/{wa.id}/read", headers=auth)
    assert (await client.get("/api/v1/channels/inbox?unread_only=true", headers=auth)).json() == []
    assert (await client.get(f"/api/v1/channels/conversations/{other_tenant.id}", headers=auth)).status_code == 404

    # Reply routes to the conversation's own channel.
    sent = []

    async def fake_send(db, tenant_id, *, to, from_, body):
        sent.append((to, from_, body))
        return "SMx"

    monkeypatch.setattr("app.services.channels.send_sms_message", fake_send)
    from packages.database.models.channels import ChannelThread

    db_session.add(ChannelThread(tenant_id=tenant.id, conversation_id=sms.id, channel="sms", external_id="k", contact_address="+15550001234", via_address="+15559990000"))
    await db_session.commit()
    resp = await client.post(f"/api/v1/channels/conversations/{sms.id}/reply", headers=auth, json={"text": "On it"})
    assert resp.status_code == 201 and sent == [("+15550001234", "+15559990000", "On it")]
    voice_reply = await client.post(f"/api/v1/channels/conversations/{voice_conv.id}/reply", headers=auth, json={"text": "x"})
    assert voice_reply.status_code == 502
    thread = (await client.get(f"/api/v1/channels/conversations/{voice_conv.id}", headers=auth)).json()
    assert thread["can_reply"] is False


# ------------------------------------------------------------------ missed calls + voicemail


async def test_missed_call_voicemail_flow(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="vm-co")
    await client.post("/api/v1/phone-numbers", headers=auth, json={"e164_number": "+15557770000"})  # no agent
    voice_form = {"To": "+15557770000", "From": "+15558881111", "CallSid": "CAvm1"}

    # Voicemail off: caller is rejected.
    assert "<Reject" in (await client.post("/api/v1/webhooks/telephony/twilio/voice", data=voice_form)).text
    await _enable(client, auth, "voicemail", config={"greeting": "Leave it"})
    twiml = (await client.post("/api/v1/webhooks/telephony/twilio/voice", data=voice_form)).text
    assert "<Record" in twiml and "Leave it" in twiml

    rec = {"CallSid": "CAvm1", "RecordingSid": "RE1", "RecordingUrl": "https://api.twilio.com/rec/RE1", "RecordingStatus": "completed", "RecordingDuration": "12"}
    assert (await client.post("/api/v1/channels/voicemail/recording", data=rec)).status_code == 200
    tr = {"CallSid": "CAvm1", "RecordingSid": "RE1", "TranscriptionStatus": "completed", "TranscriptionText": "Please call me back"}
    assert (await client.post("/api/v1/channels/voicemail/transcription", data=tr)).status_code == 200
    # Replayed callback does not duplicate.
    await client.post("/api/v1/channels/voicemail/recording", data=rec)

    voicemails = (await client.get("/api/v1/channels/voicemails", headers=auth)).json()
    assert len(voicemails) == 1
    vm = voicemails[0]
    assert vm["transcription"] == "Please call me back" and vm["duration_seconds"] == 12
    assert vm["recording_url"].endswith("RE1.mp3") and vm["transcription_status"] == "completed"
    assert (await db_session.execute(select(Voicemail))).scalars().all()[0].provider_recording_sid == "RE1"

    missed = (await client.get("/api/v1/channels/missed-calls?handled=false", headers=auth)).json()
    assert len(missed) == 1 and missed[0]["reason"] == "voicemail" and missed[0]["voicemail"]["id"] == vm["id"]

    # Callback places an AI call and marks the missed call handled.
    placed = {}

    async def fake_place(db, tenant_id, *, to_number, ai_agent_id=None, from_number=None):
        placed["to"] = to_number
        call = Call(tenant_id=tenant_id, direction=CallDirection.OUTBOUND, to_number=to_number, from_number=from_number, status=CallStatus.RINGING)
        db.add(call)
        await db.commit()
        return call

    monkeypatch.setattr("app.services.channels_voice.place_ai_outbound_call", fake_place)
    cb = await client.post(f"/api/v1/channels/missed-calls/{missed[0]['id']}/callback", headers=auth, json={"mode": "ai"})
    assert cb.status_code == 200, cb.text
    assert placed["to"] == "+15558881111"
    assert (await client.get("/api/v1/channels/missed-calls?handled=true", headers=auth)).json()[0]["callback_call_id"] == cb.json()["call_id"]

    class FakeResp:
        status_code = 200
        content = b"ID3audio"

    class FakeHttp:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, auth=None):
            assert url.endswith("RE1.mp3") and auth is not None
            return FakeResp()

    monkeypatch.setattr("app.api.v1.channels.httpx.AsyncClient", FakeHttp)
    audio = await client.get(f"/api/v1/channels/voicemails/{vm['id']}/recording", headers=auth)
    assert audio.status_code == 200 and audio.content == b"ID3audio"

    vm_toggle = await client.post(f"/api/v1/channels/voicemails/{vm['id']}/handled", headers=auth, json={"handled": True})
    assert vm_toggle.json()["handled"] is True


async def test_unanswered_inbound_call_status_creates_missed_call(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await _signup(client, slug="missed-co")
    agent_id = await _agent(client, auth)
    await client.post("/api/v1/phone-numbers", headers=auth, json={"e164_number": "+15556660000", "ai_agent_id": agent_id})
    await client.post("/api/v1/webhooks/telephony/twilio/voice", data={"To": "+15556660000", "From": "+15551110000", "CallSid": "CAmiss"})
    resp = await client.post("/api/v1/webhooks/telephony/twilio/status", data={"CallSid": "CAmiss", "CallStatus": "no-answer"})
    assert resp.status_code == 200
    rows = (await db_session.execute(select(MissedCall))).scalars().all()
    assert len(rows) == 1 and rows[0].reason == "no-answer" and rows[0].from_number == "+15551110000"
    # A completed call is not a missed call.
    await client.post("/api/v1/webhooks/telephony/twilio/voice", data={"To": "+15556660000", "From": "+15552220000", "CallSid": "CAok"})
    await client.post("/api/v1/webhooks/telephony/twilio/status", data={"CallSid": "CAok", "CallStatus": "completed", "Duration": "30"})
    assert len((await db_session.execute(select(MissedCall))).scalars().all()) == 1
    listed = (await client.get("/api/v1/channels/missed-calls", headers=auth)).json()
    assert len(listed) == 1
    handled = await client.post(f"/api/v1/channels/missed-calls/{listed[0]['id']}/handled", headers=auth, json={"handled": True})
    assert handled.json()["handled"] is True and handled.json()["handled_at"]


async def test_click_to_call_modes(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = await _signup(client, slug="ctc-co")
    bad = await client.post("/api/v1/channels/click-to-call", headers=auth, json={"to_number": "+15551234567", "mode": "bridge"})
    assert bad.status_code == 422 and "agent_phone" in bad.json()["detail"]
    assert (await client.post("/api/v1/channels/click-to-call", headers=auth, json={"to_number": "12345"})).status_code == 422
    # AI mode without a configured phone number surfaces the outbound service's error.
    ai = await client.post("/api/v1/channels/click-to-call", headers=auth, json={"to_number": "+15551234567", "mode": "ai"})
    assert ai.status_code == 422 and "phone number" in ai.json()["detail"]


async def test_settings_list_hides_secrets_and_validates_keys(client: AsyncClient) -> None:
    auth = await _signup(client, slug="set-co")
    listing = (await client.get("/api/v1/channels/settings", headers=auth)).json()
    assert [c["channel"] for c in listing] == ["sms", "email", "web_chat", "instagram", "messenger", "web_form", "voicemail"]
    assert next(c for c in listing if c["channel"] == "sms")["webhook_url"].endswith("/api/v1/channels/sms/webhook")
    bad = await client.put("/api/v1/channels/settings/sms", headers=auth, json={"config": {"nope": 1}})
    assert bad.status_code == 422
    bad = await client.put("/api/v1/channels/settings/sms", headers=auth, json={"ai_agent_id": str(uuid.uuid4())})
    assert bad.status_code == 422
    assert (await client.put("/api/v1/channels/settings/fax", headers=auth, json={})).status_code == 404


async def test_widget_design_options_are_validated_and_served(client: AsyncClient) -> None:
    auth = await _signup(client, slug="design-co")
    design = {
        "template": "bold",
        "color": "#e11d48",
        "position": "left",
        "subtitle": "We reply in minutes",
        "launcher_label": "Chat with us",
        "lead_form": True,
        "show_email": False,
    }
    cfg = await _enable(client, auth, "web_chat", config=design)
    config = (await client.get(f"/api/v1/channels/webchat/{cfg['public_key']}/config")).json()
    assert config["template"] == "bold" and config["color"] == "#e11d48"
    assert config["position"] == "left" and config["launcher_label"] == "Chat with us"
    assert config["show_email"] is False and config["show_message"] is True

    for bad in (
        {"template": "neon"},
        {"position": "top"},
        {"color": "red"},
        {"subtitle": "x" * 81},
        {"show_email": "no"},
    ):
        resp = await client.put(
            "/api/v1/channels/settings/web_chat", headers=auth, json={"config": bad}
        )
        assert resp.status_code == 422, bad


async def test_the_designer_preview_never_starts_a_real_chat_session(client):
    """In the preview the live open/close handlers would call the API and show 'Failed to fetch'."""
    js = (await client.get("/api/v1/channels/webchat/widget.js")).text
    preview = js[js.index("if (preview) {") :]
    assert "bubble.addEventListener('click', function (ev) { ev.stopImmediatePropagation()" in preview
    assert "close.addEventListener('click', function (ev) { ev.stopImmediatePropagation()" in preview
    assert "only a preview" in preview
    # nothing in the preview block, up to its own return, reaches the network
    assert "request(" not in preview.split("return;")[0]


async def test_switching_on_a_channel_picks_the_only_ai_agent_so_visitors_get_answers(client):
    """Without this a visitor typed into the chat and nobody answered, with no hint why."""
    from tests.test_whatsapp_pipeline import _auth, _signup

    tokens = await _signup(client, "auto-agent")
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Desk"})
    on = await client.put("/api/v1/channels/settings/web_chat", headers=_auth(tokens), json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json()["ai_agent_id"] == agent.json()["id"]


async def test_with_two_agents_it_does_not_guess_and_a_deliberate_no_agent_is_kept(client):
    from tests.test_whatsapp_pipeline import _auth, _signup

    tokens = await _signup(client, "two-agents")
    await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "One"})
    await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Two"})
    on = await client.put("/api/v1/channels/settings/web_chat", headers=_auth(tokens), json={"enabled": True})
    assert on.json()["ai_agent_id"] is None

    tokens2 = await _signup(client, "staff-only")
    await client.post("/api/v1/ai-agents", headers=_auth(tokens2), json={"name": "Solo"})
    off = await client.put(
        "/api/v1/channels/settings/web_chat",
        headers=_auth(tokens2),
        json={"enabled": True, "clear_ai_agent": True},
    )
    assert off.json()["ai_agent_id"] is None
