"""One customer record, a payment the workspace chooses to make, Exotel numbers already on the account,
and a real embeddings API only when one is configured."""

import uuid
from datetime import UTC, datetime

import pytest
from app.core.config import get_settings
from app.services import embeddings
from app.services.exotel import list_incoming_numbers
from httpx import AsyncClient, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    ConsentRecord,
    Conversation,
    ConversationChannel,
    Invoice,
    InvoiceStatus,
    Message,
    MessageDirection,
    MessageSender,
    Ticket,
)

from .test_channels import _signup


async def _tenant(client: AsyncClient, auth: dict) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/tenants/me", headers=auth)).json()["id"])


async def test_one_customer_page_has_the_call_the_message_the_ticket_and_the_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    auth = await _signup(client, slug="one-person")
    tenant = await _tenant(client, auth)
    made = await client.post(
        "/api/v1/customers", headers=auth, json={"name": "Asha", "phone": "+919876543210"}
    )
    assert made.status_code == 201, made.text
    customer = uuid.UUID(made.json()["id"])
    now = datetime.now(UTC)
    thread = Conversation(
        tenant_id=tenant, customer_id=customer, channel=ConversationChannel.WHATSAPP
    )
    db_session.add(thread)
    await db_session.flush()
    db_session.add_all([
        Call(
            tenant_id=tenant, customer_id=customer, direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED, duration_seconds=40, from_number="+919876543210",
            to_number="+912241491386",
        ),
        Message(
            tenant_id=tenant, conversation_id=thread.id, direction=MessageDirection.INBOUND,
            sender=MessageSender.CUSTOMER, text="The fitting is leaking",
        ),
        Ticket(tenant_id=tenant, customer_id=customer, subject="Air fitting"),
        ConsentRecord(
            tenant_id=tenant, customer_id=customer, purpose="call_back", source="widget",
            granted_at=now,
        ),
    ])
    await db_session.commit()

    body = (await client.get(f"/api/v1/customers/{customer}/dossier", headers=auth)).json()
    assert body["calls"][0]["direction"] == "inbound"
    assert body["messages"][0]["channel"] == "whatsapp" and "leaking" in body["messages"][0]["text"]
    assert body["tickets"][0]["subject"] == "Air fitting"
    assert body["consents"][0]["purpose"] == "call_back" and body["consents"][0]["status"] == "granted"


async def test_a_pending_invoice_is_paid_only_when_the_workspace_confirms_razorpay(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="pay-invoice")
    tenant = await _tenant(client, auth)
    invoice = Invoice(
        tenant_id=tenant, provider_payment_id=f"overage:{tenant}:lifetime",
        description="Usage past the allowance", amount_paise=15000, currency="INR",
        status=InvoiceStatus.PENDING, issued_at=datetime.now(UTC),
    )
    db_session.add(invoice)
    await db_session.commit()
    invoice_id = invoice.id

    async def order(*, amount_paise, receipt, notes=None):
        assert amount_paise == 15000 and notes["invoice_id"] == str(invoice_id)
        return {"id": "order_test_1"}

    async def payment(payment_id):
        assert payment_id == "pay_test_1"
        return {"status": "captured", "amount": 15000, "notes": {"invoice_id": str(invoice_id)}}

    monkeypatch.setattr("app.api.v1.billing.razorpay_create_order", order)
    monkeypatch.setattr("app.api.v1.billing.razorpay_fetch_payment", payment)
    monkeypatch.setattr("app.api.v1.billing.verify_checkout_signature", lambda **_k: True)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_key")
    get_settings.cache_clear()

    opened = await client.post(f"/api/v1/billing/invoices/{invoice_id}/checkout", headers=auth)
    assert opened.status_code == 200, opened.text
    assert opened.json()["order_id"] == "order_test_1"
    assert opened.json()["paypal_order_id"] is None

    paid = await client.post(
        f"/api/v1/billing/invoices/{invoice_id}/confirm", headers=auth,
        json={"order_id": "order_test_1", "payment_id": "pay_test_1", "signature": "sig"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid"
    await db_session.refresh(invoice)
    assert invoice.provider_payment_id == f"overage:{tenant}:lifetime"
    assert invoice.external_payment_id == "pay_test_1"


async def test_exotel_numbers_already_on_the_account_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def creds(_db, _tenant):
        return {"api_key": "k", "api_token": "t", "account_sid": "acme", "subdomain": "api.exotel.com"}

    class FakeClient:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, _url, auth=None):
            assert auth == ("k", "t")
            return Response(
                200,
                json={"IncomingPhoneNumbers": {"PhoneNumber": [
                    {"PhoneNumber": "02241491386", "FriendlyName": "Front desk"},
                ]}},
            )

    monkeypatch.setattr("app.services.exotel.get_credentials", creds)
    monkeypatch.setattr("app.services.exotel.httpx.AsyncClient", FakeClient)
    numbers = await list_incoming_numbers(None, uuid.uuid4())
    assert numbers == [{"e164": "+912241491386", "name": "Front desk"}]


def test_embeddings_stay_on_the_hasher_until_an_api_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    assert embeddings.get_embedder().name == "hash-384-v1"

    def post(url, headers=None, json=None, timeout=None):
        assert json["dimensions"] == 384
        return Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 384}]}, request=Request("POST", url))

    monkeypatch.setenv("EMBEDDINGS_API_URL", "https://embed.example/v1/embeddings")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "sk-test")
    get_settings.cache_clear()
    monkeypatch.setattr(embeddings.httpx, "post", post)
    try:
        vector = embeddings.get_embedder().embed(["price"])[0]
        assert len(vector) == 384 and vector[0] == 0.1
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
