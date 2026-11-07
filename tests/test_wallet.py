"""Prepaid wallet: rates, GST, top-ups through Razorpay, charging calls and messages from the
balance exactly once, and refusing calls when it is empty."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1 import wallet as wallet_api
from app.core.config import get_settings
from app.integrations import paypal_client, razorpay_client
from app.services import plans, wallet
from app.worker import wallet_tasks
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
    UsageEvent,
    User,
    Wallet,
    WalletTopup,
    WalletTransaction,
)
from packages.database.session import session_scope

GSTIN_MH = "27AAPFU0939F1ZV"  # a well-formed GSTIN for Maharashtra (state 27)
SECRET = "test-key-secret"
CALL_INBOUND, CALL_OUTBOUND = CallDirection.INBOUND, CallDirection.OUTBOUND


# ---- the arithmetic -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paise, expected",
    [(0, (500, 900)), (10_000, (500, 900)), (99_999, (500, 900)), (100_000, (450, 800)), (499_999, (450, 800)),
     (500_000, (400, 750)), (1_999_999, (400, 750)), (2_000_000, (350, 700)), (5_000_000, (350, 700))],
)
def test_a_bigger_top_up_buys_a_lower_rate(paise, expected):
    assert wallet.rates_for_topup(paise) == expected


def test_the_rate_table_covers_every_amount_without_gaps():
    table = wallet.rate_table()
    assert table[0]["from_paise"] == wallet.TOPUP_MIN_PAISE and table[-1]["to_paise"] == wallet.TOPUP_MAX_PAISE
    for earlier, later in zip(table, table[1:], strict=False):
        assert later["from_paise"] == earlier["to_paise"] + 100
        assert later["inbound_paise"] < earlier["inbound_paise"] and later["outbound_paise"] < earlier["outbound_paise"]


def test_gst_is_split_by_state_and_always_adds_up():
    same = wallet.gst_split(100_000, "27", "27")
    assert same == {"cgst": 9000, "sgst": 9000, "igst": 0, "gst": 18000, "total": 118000}
    other = wallet.gst_split(100_000, "29", "27")
    assert other["igst"] == 18000 and other["cgst"] == other["sgst"] == 0
    assert wallet.gst_split(100_000, None, "27")["igst"] == 18000 and wallet.gst_split(100_000, "27", None)["igst"] == 18000
    odd = wallet.gst_split(10_001, "27", "27")  # 18% of 10,001 paise does not split evenly
    assert odd["cgst"] + odd["sgst"] == odd["gst"] and odd["total"] == 10_001 + odd["gst"]


def test_gstin_is_checked_for_format_state_and_check_character():
    assert wallet.validate_gstin(f" {GSTIN_MH.lower()} ") == GSTIN_MH
    for bad in ("", "27AAPFU0939F1Z", "27AAPFU0939F1ZX", "99AAPFU0939F1ZV", "00AAPFU0939F1ZV", "ABAAPFU0939F1ZV", "27aapfu0939f1z!"):
        with pytest.raises(ValueError):
            wallet.validate_gstin(bad)


def test_invoices_number_by_financial_year():
    assert wallet.financial_year(datetime(2026, 3, 31)) == "25-26"
    assert wallet.financial_year(datetime(2026, 4, 1)) == "26-27"
    assert wallet.rupees(123456) == "₹1,234.56"


# ---- helpers --------------------------------------------------------------------------------


async def _workspace(client: AsyncClient, slug: str, tier: str = "free") -> tuple[dict, uuid.UUID]:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "Owner",
              "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        tenant.plan_tier = tier
        # Signup leaves the owner unverified. A non-legacy workspace cannot
        # spend until that email is confirmed, so these billing tests confirm it.
        owner = (await db.execute(select(User).where(User.email == f"o@{slug}.com"))).scalar_one()
        owner.email_verified_at = datetime.now(UTC)
        await db.commit()
        tenant_id = tenant.id
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}, tenant_id


@pytest.fixture
def razorpay(monkeypatch):
    orders: list[dict] = []

    async def create_order(*, amount_paise, receipt, notes=None):
        order = {"id": f"order_{uuid.uuid4().hex[:14]}", "amount": amount_paise, "receipt": receipt}
        orders.append(order)
        return order

    monkeypatch.setattr(razorpay_client, "create_order", create_order)
    monkeypatch.setattr(razorpay_client, "_auth", lambda: ("rzp_test_key", SECRET))
    monkeypatch.setattr(get_settings(), "razorpay_key_id", "rzp_test_key")
    return orders


def _signature(order_id: str, payment_id: str) -> str:
    return hmac.new(SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _profile(client, auth, **over):
    body = {"billing_name": "Demo Pvt Ltd", "gstin": GSTIN_MH, **over}
    return await client.put("/api/v1/wallet/billing-profile", headers=auth, json=body)


async def _top_up(client, auth, rupees: int, razorpay) -> dict:
    created = await client.post("/api/v1/wallet/topups", headers=auth, json={"amount_rupees": rupees})
    assert created.status_code == 201, created.text
    topup = created.json()
    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    done = await client.post(
        f"/api/v1/wallet/topups/{topup['topup_id']}/verify", headers=auth,
        json={"payment_id": payment_id, "signature": _signature(topup["order_id"], payment_id)},
    )
    assert done.status_code == 200, done.text
    return {**topup, "payment_id": payment_id, "summary": done.json()}


async def _agent(client, auth) -> str:
    resp = await client.post("/api/v1/ai-agents", headers=auth, json={"name": f"Bot {uuid.uuid4().hex[:6]}"})
    return resp.json()["id"]


# ---- top-up flow ----------------------------------------------------------------------------


async def test_a_new_workspace_sees_an_empty_wallet_and_the_rate_table(client: AsyncClient):
    auth, _ = await _workspace(client, "w-empty")
    summary = (await client.get("/api/v1/wallet", headers=auth)).json()
    assert summary["balance_paise"] == 0 and summary["can_topup"] and summary["active"] is False
    assert summary["inbound_rate_paise"] == 500 and summary["outbound_rate_paise"] == 900
    assert summary["min_topup_paise"] == 10_000 and summary["gst_percent"] == 18
    assert [r["inbound_paise"] for r in summary["rate_table"]] == [500, 450, 400, 350]
    assert (await client.get("/api/v1/wallet")).status_code == 401


async def test_a_top_up_needs_a_billing_profile_with_a_valid_gstin(client: AsyncClient, razorpay):
    auth, _ = await _workspace(client, "w-profile")
    no_profile = await client.post("/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 500})
    assert no_profile.status_code == 422 and "billing name" in no_profile.json()["detail"]
    assert (await _profile(client, auth, gstin="27AAPFU0939F1ZX")).status_code == 422
    assert (await _profile(client, auth, state_code="29")).status_code == 422  # GSTIN is Maharashtra
    saved = await _profile(client, auth)
    assert saved.status_code == 200 and saved.json()["billing_profile"] == {
        "billing_name": "Demo Pvt Ltd", "gstin": GSTIN_MH, "state_code": "27"}
    plain = await _profile(client, auth, gstin=None, state_code="08")  # no GSTIN: just a state
    assert plain.json()["billing_profile"]["gstin"] is None and plain.json()["billing_profile"]["state_code"] == "08"
    for rupees in (99, 50_001, 0):
        assert (await client.post("/api/v1/wallet/topups", headers=auth, json={"amount_rupees": rupees})).status_code == 422


async def test_topping_up_credits_the_wallet_sets_the_rate_and_issues_a_gst_invoice(client: AsyncClient, razorpay, monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_state_code", "27")
    monkeypatch.setattr(get_settings(), "seller_gstin", "27AAAAA0000A1Z5")
    auth, tenant_id = await _workspace(client, "w-topup")
    await _profile(client, auth)
    done = await _top_up(client, auth, 1_000, razorpay)
    assert done["amount_paise"] == 118_000 and done["credit_paise"] == 100_000 and done["gst_paise"] == 18_000
    assert razorpay[0]["amount"] == 118_000  # Razorpay is asked for the amount with GST
    summary = done["summary"]
    assert summary["balance_paise"] == 100_000 and summary["active"] is True
    assert (summary["inbound_rate_paise"], summary["outbound_rate_paise"]) == (450, 800)
    assert summary["minutes_left_inbound"] == 222 and summary["minutes_left_outbound"] == 125
    async with session_scope() as db:
        assert (await db.get(Tenant, tenant_id)).plan_tier == "payg"
    topups = (await client.get("/api/v1/wallet/topups", headers=auth)).json()
    assert len(topups) == 1 and topups[0]["invoice_number"].startswith("CP/") and topups[0]["invoice_number"].endswith("/000001")

    invoice = await client.get(f"/api/v1/wallet/topups/{topups[0]['id']}/invoice", headers=auth)
    assert invoice.status_code == 200 and "text/html" in invoice.headers["content-type"]
    assert "CGST 9%" in invoice.text and "SGST 9%" in invoice.text and "IGST" not in invoice.text  # same state
    assert "₹1,180.00" in invoice.text and GSTIN_MH in invoice.text and "27AAAAA0000A1Z5" in invoice.text
    second = await _top_up(client, auth, 100, razorpay)
    assert second["summary"]["balance_paise"] == 110_000  # +₹100 credit
    assert (await client.get("/api/v1/wallet/topups", headers=auth)).json()[0]["invoice_number"].endswith("/000002")
    txs = (await client.get("/api/v1/wallet/transactions", headers=auth)).json()
    assert [t["kind"] for t in txs] == ["topup", "topup"] and txs[0]["balance_after_paise"] == 110_000


async def test_another_state_pays_igst(client: AsyncClient, razorpay, monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_state_code", "08")
    auth, _ = await _workspace(client, "w-igst")
    await _profile(client, auth)
    await _top_up(client, auth, 200, razorpay)
    topup = (await client.get("/api/v1/wallet/topups", headers=auth)).json()[0]
    html = (await client.get(f"/api/v1/wallet/topups/{topup['id']}/invoice", headers=auth)).text
    assert "IGST 18%" in html and "CGST" not in html


async def test_a_wrong_signature_adds_nothing_and_a_repeat_is_harmless(client: AsyncClient, razorpay):
    auth, _ = await _workspace(client, "w-sig")
    await _profile(client, auth)
    created = (await client.post("/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 500})).json()
    url = f"/api/v1/wallet/topups/{created['topup_id']}/verify"
    forged = await client.post(url, headers=auth, json={"payment_id": "pay_1234567", "signature": "0" * 64})
    assert forged.status_code == 400
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 0
    good = {"payment_id": "pay_abcdef12", "signature": _signature(created["order_id"], "pay_abcdef12")}
    assert (await client.post(url, headers=auth, json=good)).json()["balance_paise"] == 50_000
    assert (await client.post(url, headers=auth, json=good)).json()["balance_paise"] == 50_000  # not twice
    async with session_scope() as db:
        assert (await db.execute(select(func.count()).select_from(WalletTransaction))).scalar_one() == 1


def _webhook(client, payload: dict, secret: str = "test-razorpay-webhook-secret"):
    body = json.dumps(payload)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return client.post("/api/v1/webhooks/payment/razorpay", content=body,
                       headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})


async def test_the_webhook_credits_a_payment_the_browser_never_confirmed(client: AsyncClient, razorpay):
    auth, _ = await _workspace(client, "w-hook")
    await _profile(client, auth)
    created = (await client.post("/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 500})).json()
    event = {"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_hook0001", "order_id": created["order_id"], "amount": created["amount_paise"]}}}}

    assert (await _webhook(client, event, secret="wrong")).status_code == 403
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 0
    wrong_amount = {**event, "payload": {"payment": {"entity": {**event["payload"]["payment"]["entity"], "amount": 100}}}}
    assert (await _webhook(client, wrong_amount)).status_code == 200
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 0  # amount must match
    unknown = {"event": "order.paid", "payload": {"payment": {"entity": {"id": "pay_x", "order_id": "order_nope", "amount": 1}}}}
    assert (await _webhook(client, unknown)).status_code == 200

    assert (await _webhook(client, event)).status_code == 200
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 50_000
    assert (await _webhook(client, event)).status_code == 200  # Razorpay retries
    assert (await _webhook(client, {**event, "event": "order.paid"})).status_code == 200
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 50_000


def test_paypal_amounts_reject_anything_that_is_not_rupees_and_paise() -> None:
    assert paypal_client.paise_from_amount({"value": "118.00"}) == 11800
    assert paypal_client.paise_from_amount({"value": "0.5"}) == 50
    assert paypal_client.paise_from_amount({"value": "10"}) == 1000
    assert paypal_client.paise_from_amount({"value": "1.234"}) is None
    assert paypal_client.paise_from_amount({"value": "-1.00"}) is None


async def test_a_paypal_top_up_is_captured_and_a_webhook_cannot_credit_it_twice(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "paypal_client_id", "paypal-client")
    monkeypatch.setattr(get_settings(), "paypal_client_secret", "paypal-secret")
    asked: dict = {}

    async def create_order(*, amount_paise, receipt, custom_id):
        asked["paise"] = amount_paise
        return {"id": "PAYORDER1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=PAYORDER1"}

    def _order(order_id: str) -> dict:
        return {
            "id": order_id,
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {"captures": [{
                    "id": "CAP1",
                    "status": "COMPLETED",
                    "amount": {"currency_code": "INR", "value": f"{asked['paise'] / 100:.2f}"},
                }]},
            }],
        }

    async def capture_order(order_id):
        return _order(order_id)

    async def verify_webhook(headers, event):
        return headers.get("paypal-transmission-id") == "good"

    monkeypatch.setattr(paypal_client, "create_order", create_order)
    monkeypatch.setattr(paypal_client, "capture_order", capture_order)
    monkeypatch.setattr(paypal_client, "verify_webhook", verify_webhook)

    auth, _tenant = await _workspace(client, "w-paypal")
    await _profile(client, auth)
    missing = await client.post(
        "/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 100, "provider": "nope"}
    )
    assert missing.status_code == 422

    created = await client.post(
        "/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 100, "provider": "paypal"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["provider"] == "paypal"
    assert body["client_id"] == "paypal-client"
    assert body["order_id"] == "PAYORDER1"
    assert body["approve_url"].startswith("https://www.sandbox.paypal.com/")
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 0
    razorpay_confirm = await client.post(
        f"/api/v1/wallet/topups/{body['topup_id']}/verify",
        headers=auth,
        json={"payment_id": "pay_paypalxx", "signature": "0" * 16},
    )
    assert razorpay_confirm.status_code == 400

    captured = await client.post(f"/api/v1/wallet/topups/{body['topup_id']}/capture", headers=auth)
    assert captured.status_code == 200, captured.text
    assert captured.json()["balance_paise"] == 10_000
    again = await client.post(f"/api/v1/wallet/topups/{body['topup_id']}/capture", headers=auth)
    assert again.json()["balance_paise"] == 10_000

    event = {
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP1",
            "amount": {"currency_code": "INR", "value": f"{body['amount_paise'] / 100:.2f}"},
            "supplementary_data": {"related_ids": {"order_id": "PAYORDER1"}},
        },
    }
    forged = await client.post("/api/v1/webhooks/payment/paypal", json=event)
    assert forged.status_code == 403
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 10_000
    short = json.loads(json.dumps(event))
    short["resource"]["amount"]["value"] = "1.00"
    short["resource"]["id"] = "CAPSHORT"
    refused = await client.post(
        "/api/v1/webhooks/payment/paypal",
        content=json.dumps(short),
        headers={"paypal-transmission-id": "good", "Content-Type": "application/json"},
    )
    assert refused.status_code == 200
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 10_000
    replay = await client.post(
        "/api/v1/webhooks/payment/paypal",
        content=json.dumps(event),
        headers={"paypal-transmission-id": "good", "Content-Type": "application/json"},
    )
    assert replay.status_code == 200
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 10_000


async def test_an_approved_paypal_order_is_captured_by_the_webhook(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "paypal_client_id", "paypal-client")
    monkeypatch.setattr(get_settings(), "paypal_client_secret", "paypal-secret")
    asked: dict = {}

    async def create_order(*, amount_paise, receipt, custom_id):
        asked["paise"] = amount_paise
        return {"id": "PAYORDER2", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=PAYORDER2"}

    async def capture_order(order_id):
        return {
            "id": order_id,
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {"captures": [{
                    "id": "CAP2",
                    "status": "COMPLETED",
                    "amount": {"currency_code": "INR", "value": f"{asked['paise'] / 100:.2f}"},
                }]},
            }],
        }

    async def verify_webhook(headers, event):
        return True

    monkeypatch.setattr(paypal_client, "create_order", create_order)
    monkeypatch.setattr(paypal_client, "capture_order", capture_order)
    monkeypatch.setattr(paypal_client, "verify_webhook", verify_webhook)
    auth, _tenant = await _workspace(client, "w-paypal-hook")
    await _profile(client, auth)
    created = await client.post(
        "/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 100, "provider": "paypal"}
    )
    assert created.status_code == 201, created.text
    approved = await client.post(
        "/api/v1/webhooks/payment/paypal",
        json={"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "PAYORDER2"}},
        headers={"paypal-transmission-id": "good"},
    )
    assert approved.status_code == 200, approved.text
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 10_000


async def test_paypal_without_credentials_is_refused(client: AsyncClient) -> None:
    auth, _tenant = await _workspace(client, "w-paypal-off")
    await _profile(client, auth)
    refused = await client.post(
        "/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 100, "provider": "paypal"}
    )
    assert refused.status_code == 424
    assert "PAYPAL_CLIENT_ID" in refused.json()["detail"]


async def test_only_free_and_pay_as_you_go_workspaces_can_top_up_and_nobody_else_can_see_a_top_up(client: AsyncClient, razorpay):
    legacy, _ = await _workspace(client, "w-legacy", tier="legacy")
    growth, _ = await _workspace(client, "w-growth", tier="growth")
    for auth in (legacy, growth):
        assert (await client.get("/api/v1/wallet", headers=auth)).json()["can_topup"] is False
        await _profile(client, auth)
        assert (await client.post("/api/v1/wallet/topups", headers=auth, json={"amount_rupees": 500})).status_code == 409

    mine, _ = await _workspace(client, "w-mine")
    other, _ = await _workspace(client, "w-other")
    await _profile(client, mine)
    created = (await client.post("/api/v1/wallet/topups", headers=mine, json={"amount_rupees": 500})).json()
    stolen = await client.post(f"/api/v1/wallet/topups/{created['topup_id']}/verify", headers=other,
                               json={"payment_id": "pay_stolen1", "signature": _signature(created["order_id"], "pay_stolen1")})
    assert stolen.status_code == 404
    assert (await client.get(f"/api/v1/wallet/topups/{created['topup_id']}/invoice", headers=other)).status_code == 404
    assert (await client.get(f"/api/v1/wallet/topups/{created['topup_id']}/invoice", headers=mine)).status_code == 404  # unpaid


async def test_the_invoice_escapes_what_the_customer_typed(client: AsyncClient, razorpay):
    auth, _ = await _workspace(client, "w-escape")
    await _profile(client, auth, billing_name='<script>alert(1)</script> & Co')
    await _top_up(client, auth, 100, razorpay)
    topup = (await client.get("/api/v1/wallet/topups", headers=auth)).json()[0]
    html = (await client.get(f"/api/v1/wallet/topups/{topup['id']}/invoice", headers=auth)).text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


# ---- charging usage -------------------------------------------------------------------------


async def _call(tenant_id, agent_id, *, seconds: int, direction=CALL_OUTBOUND, when=None, with_agent=True) -> uuid.UUID:
    async with session_scope() as db:
        call = Call(tenant_id=tenant_id, direction=direction, status=CallStatus.COMPLETED,
                    ai_agent_id=uuid.UUID(agent_id) if with_agent else None, duration_seconds=seconds,
                    started_at=when or datetime.now(UTC))
        db.add(call)
        await db.commit()
        return call.id


async def _balance(tenant_id) -> int:
    async with session_scope() as db:
        return (await wallet.get_wallet(db, tenant_id)).balance_paise


async def test_calls_are_charged_by_whole_minutes_at_the_rate_for_their_direction_exactly_once(client: AsyncClient, razorpay):
    auth, tenant_id = await _workspace(client, "w-calls")
    await _profile(client, auth)
    await _top_up(client, auth, 1_000, razorpay)  # 450 in / 800 out per minute, balance 100,000
    agent = await _agent(client, auth)
    await _call(tenant_id, agent, seconds=61, direction=CALL_INBOUND)   # 2 min x 450 = 900
    await _call(tenant_id, agent, seconds=30, direction=CALL_OUTBOUND)  # 1 min x 800 = 800
    await _call(tenant_id, agent, seconds=0)                            # never connected: free
    await _call(tenant_id, agent, seconds=600, with_agent=False)        # a human call: not billed here
    await _call(tenant_id, agent, seconds=300, when=datetime.now(UTC) - timedelta(days=3))  # before the wallet

    summary = (await client.get("/api/v1/wallet", headers=auth)).json()
    assert summary["balance_paise"] == 100_000 - 900 - 800
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 98_300  # no double charge
    async with session_scope() as db:
        assert await wallet.settle(db, tenant_id) == 0
    txs = (await client.get("/api/v1/wallet/transactions", headers=auth)).json()
    assert sorted(t["amount_paise"] for t in txs if t["kind"] == "call") == [-900, -800]
    assert {t["description"] for t in txs if t["kind"] == "call"} == {"Inbound call, 2 min", "Outbound call, 1 min"}
    assert min(t["balance_after_paise"] for t in txs) == 98_300


async def test_messages_are_charged_from_message_rows_and_usage_events(client: AsyncClient, razorpay):
    auth, tenant_id = await _workspace(client, "w-msgs")
    await _profile(client, auth)
    await _top_up(client, auth, 500, razorpay)
    async with session_scope() as db:
        conversation = Conversation(tenant_id=tenant_id, channel=ConversationChannel.WHATSAPP)
        db.add(conversation)
        await db.flush()
        for direction in (MessageDirection.OUTBOUND, MessageDirection.OUTBOUND, MessageDirection.INBOUND):
            db.add(Message(tenant_id=tenant_id, conversation_id=conversation.id, direction=direction,
                           sender=MessageSender.AI_AGENT if direction == MessageDirection.OUTBOUND else MessageSender.CUSTOMER,
                           text="hi"))
        db.add(UsageEvent(tenant_id=tenant_id, metric="whatsapp_messages", quantity=1, occurred_at=datetime.now(UTC)))
        db.add(UsageEvent(tenant_id=tenant_id, metric="sms", quantity=3, occurred_at=datetime.now(UTC)))
        db.add(UsageEvent(tenant_id=tenant_id, metric="social", quantity=2, occurred_at=datetime.now(UTC)))
        db.add(UsageEvent(tenant_id=tenant_id, metric="storage_bytes", quantity=999, occurred_at=datetime.now(UTC)))
        await db.commit()
    # 2 WhatsApp rows + 1 WhatsApp event, 3 SMS, 2 Instagram/Messenger. Storage is not a charge.
    spent = (
        3 * wallet.WHATSAPP_MESSAGE_PAISE
        + 3 * wallet.SMS_PAISE
        + 2 * wallet.SOCIAL_PAISE
    )
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 50_000 - spent
    assert (await client.get("/api/v1/wallet", headers=auth)).json()["balance_paise"] == 50_000 - spent


async def test_the_ledger_refuses_a_second_charge_for_the_same_thing(client: AsyncClient, razorpay):
    auth, tenant_id = await _workspace(client, "w-unique")
    await _profile(client, auth)
    await _top_up(client, auth, 100, razorpay)
    ref = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with session_scope() as db:
            for _ in range(2):
                db.add(WalletTransaction(tenant_id=tenant_id, kind="call", amount_paise=-1, balance_after_paise=0,
                                         reference_id=ref, description="x", detail={}))
            await db.commit()


async def test_the_scheduled_job_charges_every_wallet(client: AsyncClient, razorpay):
    auth, tenant_id = await _workspace(client, "w-job")
    await _profile(client, auth)
    await _top_up(client, auth, 1_000, razorpay)
    await _call(tenant_id, await _agent(client, auth), seconds=60, direction=CALL_INBOUND)
    assert await wallet_tasks._settle_all() == 450
    assert await wallet_tasks._settle_all() == 0
    assert await _balance(tenant_id) == 100_000 - 450


# ---- gates ----------------------------------------------------------------------------------


async def test_an_empty_wallet_refuses_calls_and_messages_and_a_top_up_reopens_them(client: AsyncClient, razorpay):
    auth, tenant_id = await _workspace(client, "w-gate")
    await _profile(client, auth)
    async with session_scope() as db:  # on pay-as-you-go but nothing paid in yet
        await wallet.ensure_wallet(db, tenant_id)
        (await db.get(Tenant, tenant_id)).plan_tier = "payg"
        await db.commit()
    async with session_scope() as db:
        for check in (plans.ensure_minutes_available(db, tenant_id), plans.ensure_outbound_allowed(db, tenant_id),
                      plans.ensure_inbound_allowed(db, tenant_id), plans.ensure_whatsapp_available(db, tenant_id)):
            with pytest.raises(plans.PlanLimitError) as err:
                await check
            assert err.value.code == "wallet_empty" and err.value.http_status == 402 and "Top up" in err.value.message

    await _top_up(client, auth, 100, razorpay)
    async with session_scope() as db:
        await plans.ensure_minutes_available(db, tenant_id)
        await plans.ensure_whatsapp_available(db, tenant_id, count=3)
        # 10,000 paise at 900/min outbound is 11 minutes; the call may not outrun the balance
        assert await plans.effective_max_call_seconds(db, tenant_id, 1_800) == 11 * 60
        assert await plans.effective_max_call_seconds(db, tenant_id, 120) == 120


async def test_a_balance_the_last_call_overspent_blocks_the_next_one(client: AsyncClient, razorpay):
    auth, tenant_id = await _workspace(client, "w-over")
    await _profile(client, auth)
    await _top_up(client, auth, 100, razorpay)  # 10,000 paise at 500/900
    await _call(tenant_id, await _agent(client, auth), seconds=15 * 60, direction=CALL_OUTBOUND)  # 15 x 900 = 13,500
    summary = (await client.get("/api/v1/wallet", headers=auth)).json()
    assert summary["balance_paise"] == 10_000 - 13_500 and summary["low"] is True and summary["minutes_left_outbound"] == 0
    async with session_scope() as db:
        with pytest.raises(plans.PlanLimitError):
            await plans.ensure_minutes_available(db, tenant_id)
    await _top_up(client, auth, 100, razorpay)
    async with session_scope() as db:
        await plans.ensure_minutes_available(db, tenant_id)


async def test_other_plans_are_not_affected_by_the_wallet(client: AsyncClient):
    _, tenant_id = await _workspace(client, "w-plain", tier="legacy")
    async with session_scope() as db:
        await plans.ensure_minutes_available(db, tenant_id)
        await plans.ensure_whatsapp_available(db, tenant_id)
        assert await plans.effective_max_call_seconds(db, tenant_id, 300) == 300
        assert (await db.execute(select(func.count()).select_from(Wallet))).scalar_one() == 0


def test_the_pay_as_you_go_tier_is_not_listed_as_a_plan_to_buy():
    assert "payg" in plans.TIERS and all(t["code"] != "payg" for t in plans.public_catalogue())
    _ = (wallet_api, WalletTopup)


async def test_two_first_requests_at_once_create_one_wallet_and_neither_fails(client: AsyncClient):
    import asyncio

    _, tenant_id = await _workspace(client, "w-race")

    async def first_use():
        async with session_scope() as db:
            w = await wallet.ensure_wallet(db, tenant_id)
            await db.commit()
            return w.id

    ids = await asyncio.gather(*(first_use() for _ in range(6)))
    assert len(set(ids)) == 1
    async with session_scope() as db:
        assert (await db.execute(select(func.count()).select_from(Wallet))).scalar_one() == 1
