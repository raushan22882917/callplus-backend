"""After a call: message the customer on WhatsApp, SMS or email from the inbox."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import get_settings
from app.services import follow_up
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import (
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageSender,
    PhoneNumber,
    WhatsAppTemplate,
)
from packages.database.session import session_scope
from tests.test_whatsapp_pipeline import _auth, _signup


@pytest.fixture
def sent(monkeypatch):
    """Records what would have gone out, instead of calling Meta, Twilio or an SMTP server."""
    log: list[tuple] = []

    async def wa(account, *, to, body, metered=True):
        log.append(("whatsapp", to, body))
        return {"messages": [{"id": "wamid.TEST"}]}

    async def sms(db, tenant_id, *, to, from_, body):
        log.append(("sms", to, from_, body))
        return "SM_TEST"

    async def email(*, to_email, subject, body, in_reply_to, references, reply_to, **_workspace):
        log.append(("email", to_email, subject, body))
        return "<follow-up-1@callpulse.test>"

    monkeypatch.setattr(follow_up, "send_text_message", wa)
    monkeypatch.setattr(follow_up, "send_sms_message", sms)
    monkeypatch.setattr(follow_up, "send_email_message", email)
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.test")
    return log


async def _after_a_call(
    client: AsyncClient, slug: str, *, phone="+919800000001", email="rahul@example.com"
):
    """A workspace with a finished call from one customer."""
    tokens = await _signup(client, slug)
    me = (await client.get("/api/v1/users/me", headers=_auth(tokens))).json()
    body = {"name": "Rahul", "phone": phone}
    if email:
        body["email"] = email
    customer = (await client.post("/api/v1/customers", headers=_auth(tokens), json=body)).json()
    async with session_scope() as db:
        call = Conversation(
            tenant_id=uuid.UUID(me["tenant_id"]),
            customer_id=uuid.UUID(customer["id"]),
            channel=ConversationChannel.VOICE,
        )
        db.add(call)
        await db.commit()
        return tokens, uuid.UUID(me["tenant_id"]), call.id, customer["id"]


async def _connect_whatsapp(client, tokens):
    r = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={
            "phone_number_id": "PN1",
            "waba_id": "W1",
            "display_phone_number": "+919000000000",
            "access_token": "t",
        },
    )
    assert r.status_code == 201, r.text


async def _enable_sms(client, tokens, tenant_id):
    async with session_scope() as db:
        db.add(PhoneNumber(tenant_id=tenant_id, e164_number="+918000000009"))
        await db.commit()
    r = await client.put(
        "/api/v1/channels/settings/sms", headers=_auth(tokens), json={"enabled": True}
    )
    assert r.status_code == 200, r.text


async def _messages(conversation_id):
    async with session_scope() as db:
        return list(
            (
                await db.execute(select(Message).where(Message.conversation_id == conversation_id))
            ).scalars()
        )


# ---- what is possible, and why not ------------------------------------------------------------


async def test_options_say_plainly_what_is_missing(client: AsyncClient, sent):
    tokens, _, call, _ = await _after_a_call(client, "acme", email=None)
    opts = (
        await client.get(f"/api/v1/channels/conversations/{call}/follow-up", headers=_auth(tokens))
    ).json()
    assert opts["customer"]["phone"] == "+919800000001"
    assert (
        opts["whatsapp"]["available"] is False and "Connect WhatsApp" in opts["whatsapp"]["reason"]
    )
    assert opts["sms"]["available"] is False and "Turn on SMS" in opts["sms"]["reason"]
    assert opts["email"]["available"] is False and "No email address" in opts["email"]["reason"]


async def test_options_light_up_as_things_are_connected(client: AsyncClient, sent):
    tokens, tenant_id, call, _ = await _after_a_call(client, "acme")
    await _connect_whatsapp(client, tokens)
    await _enable_sms(client, tokens, tenant_id)
    opts = (
        await client.get(f"/api/v1/channels/conversations/{call}/follow-up", headers=_auth(tokens))
    ).json()
    assert opts["whatsapp"]["available"] and opts["sms"]["available"] and opts["email"]["available"]
    assert opts["whatsapp"]["window_open"] is False  # they only called; no WhatsApp message yet


# ---- sending ----------------------------------------------------------------------------------


async def test_sms_after_a_call_goes_to_the_customers_number_and_is_kept_in_an_sms_thread(
    client: AsyncClient, sent
):
    tokens, tenant_id, call, _ = await _after_a_call(client, "acme")
    await _enable_sms(client, tokens, tenant_id)
    resp = await client.post(
        f"/api/v1/channels/conversations/{call}/follow-up",
        headers=_auth(tokens),
        json={"via": "sms", "text": "Thanks for calling Apna Salon!"},
    )
    assert resp.status_code == 201, resp.text
    assert sent == [("sms", "+919800000001", "+918000000009", "Thanks for calling Apna Salon!")]
    body = resp.json()
    assert body["channel"] == "sms" and body["conversation_id"] != str(call)
    saved = await _messages(uuid.UUID(body["conversation_id"]))
    assert [(m.direction, m.sender, m.text) for m in saved] == [
        (MessageDirection.OUTBOUND, MessageSender.HUMAN_AGENT, "Thanks for calling Apna Salon!")
    ]
    assert await _messages(call) == []  # the call thread itself is untouched


async def test_email_after_a_call_uses_a_default_subject_and_keeps_the_message_id_for_replies(
    client: AsyncClient, sent
):
    tokens, tenant_id, call, _ = await _after_a_call(client, "acme")
    resp = await client.post(
        f"/api/v1/channels/conversations/{call}/follow-up",
        headers=_auth(tokens),
        json={"via": "email", "text": "Here is the summary of our call."},
    )
    assert resp.status_code == 201, resp.text
    assert sent == [
        (
            "email",
            "rahul@example.com",
            "Following up on your call",
            "Here is the summary of our call.",
        )
    ]
    saved = await _messages(uuid.UUID(resp.json()["conversation_id"]))
    assert saved[0].provider_message_id == "<follow-up-1@callpulse.test>"

    # The customer's reply quotes our Message-ID, and the inbound email path finds this same thread.
    from app.services import channels as svc

    async with session_scope() as db:
        found = await svc.find_email_conversation_id(
            db, tenant_id, ["<follow-up-1@callpulse.test>"]
        )
    assert str(found) == resp.json()["conversation_id"]


async def test_whatsapp_free_text_needs_the_customer_to_have_written_in_the_last_24_hours(
    client: AsyncClient, sent
):
    tokens, tenant_id, call, _ = await _after_a_call(client, "acme")
    await _connect_whatsapp(client, tokens)
    refused = await client.post(
        f"/api/v1/channels/conversations/{call}/follow-up",
        headers=_auth(tokens),
        json={"via": "whatsapp", "text": "Hello!"},
    )
    assert refused.status_code == 422
    assert "24 hours" in refused.json()["detail"] and "template" in refused.json()["detail"]
    assert sent == []  # nothing was attempted

    # Once they have written to us, free text is fine and goes into the same WhatsApp thread.
    async with session_scope() as db:
        wa = Conversation(
            tenant_id=tenant_id, customer_id=None, channel=ConversationChannel.WHATSAPP
        )
        db.add(wa)
        await db.flush()
        from packages.database.models import ChannelThread

        db.add(
            ChannelThread(
                tenant_id=tenant_id,
                conversation_id=wa.id,
                channel="whatsapp",
                external_id="919800000001",
                contact_address="919800000001",
                via_address="PN1",
            )
        )
        db.add(
            Message(
                tenant_id=tenant_id,
                conversation_id=wa.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.CUSTOMER,
                text="hi",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await db.commit()
        wa_id = wa.id
    ok = await client.post(
        f"/api/v1/channels/conversations/{call}/follow-up",
        headers=_auth(tokens),
        json={"via": "whatsapp", "text": "Hello!"},
    )
    assert ok.status_code == 201, ok.text
    assert sent == [("whatsapp", "919800000001", "Hello!")]
    assert ok.json()["conversation_id"] == str(wa_id)  # their answer will land in the same thread


async def test_whatsapp_template_can_start_the_chat_after_a_call(
    client: AsyncClient, sent, monkeypatch
):
    tokens, tenant_id, call, _ = await _after_a_call(client, "acme")
    await _connect_whatsapp(client, tokens)
    async with session_scope() as db:
        template = WhatsAppTemplate(
            tenant_id=tenant_id,
            name="thanks_for_calling",
            language="en",
            category="UTILITY",
            status="APPROVED",
            purpose="custom",
            body="Hi {{name}}, thanks for calling.",
            variables=["name"],
            buttons=[],
        )
        db.add(template)
        await db.commit()
        template_id = template.id

    calls: list = []

    async def fake_send_template(
        db, tenant_id, *, to, template, values, account=None, metered=True
    ):
        calls.append((to, template.name, values.get("name")))
        return "wamid.TPL"

    monkeypatch.setattr(follow_up.tpl, "send_template", fake_send_template)
    opts = (
        await client.get(f"/api/v1/channels/conversations/{call}/follow-up", headers=_auth(tokens))
    ).json()
    assert [t["name"] for t in opts["whatsapp"]["templates"]] == ["thanks_for_calling"]
    resp = await client.post(
        f"/api/v1/channels/conversations/{call}/follow-up",
        headers=_auth(tokens),
        json={"via": "whatsapp", "template_id": str(template_id)},
    )
    assert resp.status_code == 201, resp.text
    assert calls == [
        ("919800000001", "thanks_for_calling", "Rahul")
    ]  # the blank filled from the customer
    assert "Hi Rahul" in resp.json()["message"]["text"]


# ---- refusals, in plain words -----------------------------------------------------------------


async def test_each_missing_piece_is_refused_in_plain_words(client: AsyncClient, sent):
    tokens, _, call, _ = await _after_a_call(client, "acme", email=None)

    def post(**body):
        return client.post(
            f"/api/v1/channels/conversations/{call}/follow-up", headers=_auth(tokens), json=body
        )

    no_mail = await post(via="email", text="hi")
    no_sms = await post(via="sms", text="hi")
    no_wa = await post(via="whatsapp", text="hi")
    empty = await post(via="sms", text="   ")
    bad = await post(via="fax", text="hi")
    assert "No email address" in no_mail.json()["detail"]
    assert "Turn on SMS" in no_sms.json()["detail"]
    assert "Connect WhatsApp" in no_wa.json()["detail"]
    assert "Write a message" in empty.json()["detail"]
    assert bad.status_code == 422
    assert sent == []


async def test_a_conversation_with_no_customer_cannot_be_followed_up(client: AsyncClient, sent):
    tokens, tenant_id, _, _ = await _after_a_call(client, "acme")
    async with session_scope() as db:
        orphan = Conversation(
            tenant_id=tenant_id, customer_id=None, channel=ConversationChannel.VOICE
        )
        db.add(orphan)
        await db.commit()
        orphan_id = orphan.id
    resp = await client.post(
        f"/api/v1/channels/conversations/{orphan_id}/follow-up",
        headers=_auth(tokens),
        json={"via": "email", "text": "hi"},
    )
    assert resp.status_code == 422 and "not linked to a customer" in resp.json()["detail"]


async def test_another_workspaces_conversation_is_not_reachable(client: AsyncClient, sent):
    _, _, call_a, _ = await _after_a_call(client, "alpha", phone="+919800000001")
    tokens_b, *_ = await _after_a_call(client, "beta", phone="+919800000002")
    get = await client.get(
        f"/api/v1/channels/conversations/{call_a}/follow-up", headers=_auth(tokens_b)
    )
    post = await client.post(
        f"/api/v1/channels/conversations/{call_a}/follow-up",
        headers=_auth(tokens_b),
        json={"via": "email", "text": "hi"},
    )
    assert get.status_code == 404 and post.status_code == 404
    assert sent == []


async def test_it_needs_a_signed_in_user(client: AsyncClient):
    nobody = uuid.uuid4()
    assert (await client.get(f"/api/v1/channels/conversations/{nobody}/follow-up")).status_code in (
        401,
        403,
    )
    assert (
        await client.post(
            f"/api/v1/channels/conversations/{nobody}/follow-up", json={"via": "sms", "text": "x"}
        )
    ).status_code in (401, 403)
