"""Channel packs: Call, WhatsApp and Email bought one by one or all three together. Server-side pricing,
payment proof before any allowance, one-time crediting, per-pack windows, and the plan checks that refuse a
channel nobody paid for."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import get_settings
from app.integrations import razorpay_client
from app.services import packs, plans
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import PackPurchase, Tenant, UsageEvent, User
from packages.database.session import session_scope

SECRET = "test-key-secret"
GSTIN_MH = "27AAPFU0939F1ZV"  # well-formed, Maharashtra (state 27)
CALL, WA, MAIL = 219_900, 89_900, 79_900  # paise, before GST


# ---- prices ---------------------------------------------------------------------------------


def test_each_pack_is_priced_on_its_own_and_only_all_three_get_the_bundle_discount() -> None:
    assert packs.quote(["call"])["amount_paise"] == CALL
    two = packs.quote(["whatsapp", "call", "whatsapp"])  # order and repeats do not matter
    assert two["items"] == ["call", "whatsapp"] and two["amount_paise"] == CALL + WA and two["bundle"] is False
    assert two["discount_paise"] == 0
    bundle = packs.quote(["email_sms", "call", "whatsapp"])
    full = CALL + WA + MAIL
    assert bundle["bundle"] is True and bundle["list_price_paise"] == full
    assert bundle["discount_paise"] == round(full * 0.10) and bundle["amount_paise"] == full - round(full * 0.10)


def test_a_bad_choice_is_refused_and_the_discount_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ([], ["telegram"], ["call", "x"]):
        with pytest.raises(packs.PackError):
            packs.quote(bad)
    monkeypatch.setattr(get_settings(), "pack_bundle_discount_percent", 99)
    assert packs.bundle_discount_percent() == 50
    monkeypatch.setattr(get_settings(), "pack_bundle_discount_percent", -5)
    assert packs.bundle_discount_percent() == 0


def test_the_catalogue_shows_gst_and_the_bundle_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "seller_state_code", "27")
    same_state = packs.catalogue("27")
    assert [p["code"] for p in same_state["packs"]] == ["call", "whatsapp", "email_sms"]
    call = same_state["packs"][0]
    assert call["price_paise"] == CALL and call["gst_paise"] == round(CALL * 0.18) and call["total_paise"] == CALL + call["gst_paise"]
    assert same_state["bundle"]["discount_percent"] == 10 and same_state["bundle"]["discount_paise"] > 0


def _tenant(state: dict | None = None, tier: str = "packs") -> Tenant:
    return Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}", plan_tier=tier, pack_state=state or {})


def _entry(days_left: float, **grant) -> dict:
    now = datetime.now(UTC)
    return {"start": (now - timedelta(days=30 - days_left)).isoformat(),
            "end": (now + timedelta(days=days_left)).isoformat(), **grant}


def test_the_tier_only_grants_what_was_paid_for_and_ignores_expired_packs() -> None:
    nothing = plans.effective(_tenant())
    assert (nothing.ai_minutes, nothing.whatsapp_messages, nothing.email_messages, nothing.sms_messages) == (0, 0, 0, 0)
    assert nothing.campaigns is False and nothing.recording is False

    tier = plans.effective(_tenant({"call": _entry(10, ai_minutes=250), "whatsapp": _entry(-1, whatsapp_messages=1000)}))
    assert tier.ai_minutes == 250 and tier.recording is True and tier.campaigns is True and tier.phone_numbers == 2
    assert tier.whatsapp_messages == 0  # that pack ended yesterday
    assert tier.email_messages == 0

    mail = plans.effective(_tenant({"email_sms": _entry(5, email_messages=3000)}))
    assert (mail.email_messages, mail.sms_messages, mail.ai_minutes) == (3000, 0, 0)  # the Email pack has no SMS


# ---- crediting ------------------------------------------------------------------------------


async def _purchase(db, tenant_id: uuid.UUID, items: list[str], **over) -> PackPurchase:
    q = packs.quote(items)
    row = PackPurchase(
        tenant_id=tenant_id, provider="razorpay", provider_order_id=f"order_{uuid.uuid4().hex[:14]}", status="created",
        items=q["items"], bundle=q["bundle"], allowances=q["allowances"], period_days=30, amount_paise=q["amount_paise"],
        discount_paise=q["discount_paise"], igst_paise=round(q["amount_paise"] * 0.18),
        total_paise=q["amount_paise"] + round(q["amount_paise"] * 0.18), billing_name="Demo", **over,
    )
    db.add(row)
    await db.flush()
    return row


async def _free_tenant(db, tier: str = "free") -> Tenant:
    tenant = Tenant(name="Shop", slug=f"shop-{uuid.uuid4().hex[:8]}", plan_tier=tier)
    db.add(tenant)
    await db.flush()
    return tenant


async def test_a_paid_order_starts_the_packs_once_and_a_repeat_changes_nothing() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db)
        buy = await _purchase(db, tenant.id, ["call", "whatsapp"])
        assert await packs.credit_purchase(db, buy, "pay_1") is True
        assert await packs.credit_purchase(db, buy, "pay_1") is False  # a webhook after the browser
        await db.commit()
        await db.refresh(tenant)
        assert tenant.plan_tier == "packs"
        held = packs.active(tenant)
        assert set(held) == {"call", "whatsapp"} and held["call"]["ai_minutes"] == 250
        assert 29 < (held["call"]["end"] - held["call"]["start"]).days + 1 <= 31
        assert buy.status == "paid" and buy.provider_payment_id == "pay_1"
        assert buy.invoice_number.startswith("CPK/") and buy.invoice_number.endswith("000001")


async def test_buying_a_pack_again_early_extends_it_and_adds_the_allowance() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db)
        first = await _purchase(db, tenant.id, ["call"])
        await packs.credit_purchase(db, first, "pay_a")
        await db.refresh(tenant)
        before = packs.active(tenant)["call"]
        second = await _purchase(db, tenant.id, ["call"])
        await packs.credit_purchase(db, second, "pay_b")
        await db.refresh(tenant)
        after = packs.active(tenant)["call"]
        assert after["ai_minutes"] == 500 and after["end"] - before["end"] == timedelta(days=30)
        assert after["start"] == before["start"] and second.invoice_number.endswith("000002")


async def test_an_expired_pack_restarts_from_today_with_a_fresh_allowance() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db, "packs")
        tenant.pack_state = {"whatsapp": _entry(-3, whatsapp_messages=1000)}
        buy = await _purchase(db, tenant.id, ["whatsapp"])
        await packs.credit_purchase(db, buy, "pay_c")
        await db.refresh(tenant)
        entry = packs.active(tenant)["whatsapp"]
        assert entry["whatsapp_messages"] == 1000 and entry["start"] > datetime.now(UTC) - timedelta(minutes=1)


async def test_a_payment_for_the_wrong_amount_or_provider_credits_nothing() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db)
        buy = await _purchase(db, tenant.id, ["call"])
        total, order = buy.total_paise, buy.provider_order_id
        assert await packs.credit_paid_order(db, order_id=order, payment_id="p", paid_paise=total - 1, provider="razorpay") is None
        assert await packs.credit_paid_order(db, order_id=order, payment_id="p", paid_paise=total, provider="paypal") is None
        assert await packs.credit_paid_order(db, order_id="order_unknown", payment_id="p", paid_paise=total, provider="razorpay") is None
        assert await packs.credit_paid_order(db, order_id=order, payment_id="", paid_paise=total, provider="razorpay") is None
        await db.refresh(tenant)
        assert tenant.pack_state == {} and tenant.plan_tier == "free"
        assert await packs.credit_paid_order(db, order_id=order, payment_id="p", paid_paise=total, provider="razorpay") is not None


# ---- the plan checks ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _verified(monkeypatch: pytest.MonkeyPatch):
    async def ok(db, tenant):
        return None

    monkeypatch.setattr(plans, "ensure_owner_verified", ok)


async def _event(db, tenant_id, metric: str, when: datetime, quantity: int = 1) -> None:
    db.add(UsageEvent(tenant_id=tenant_id, metric=metric, quantity=quantity, occurred_at=when, extra={}))


async def test_a_channel_nobody_paid_for_is_refused_with_the_pack_to_buy() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db, "packs")
        tenant.pack_state = {"whatsapp": _entry(20, whatsapp_messages=1000)}
        await db.commit()
        await plans.ensure_whatsapp_available(db, tenant.id)  # paid for: allowed
        for call, pack_name in ((plans.ensure_email_available(db, tenant.id), "Email pack"),
                                (plans.ensure_minutes_available(db, tenant.id), "Call pack")):
            with pytest.raises(plans.PlanLimitError) as err:
                await call
            assert err.value.code == "pack_required" and pack_name in str(err.value) and err.value.http_status == 402


async def test_the_allowance_counts_only_inside_the_packs_own_window(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db, "packs")
        entry = _entry(20, whatsapp_messages=3)
        tenant.pack_state = {"whatsapp": entry}
        start = datetime.fromisoformat(entry["start"])
        await _event(db, tenant.id, "whatsapp_messages", start - timedelta(days=2), 50)  # before the pack: not counted
        for _ in range(3):
            await _event(db, tenant.id, "whatsapp_messages", start + timedelta(hours=1))
        await db.commit()
        with pytest.raises(plans.PlanLimitError) as err:
            await plans.ensure_whatsapp_available(db, tenant.id)
        assert err.value.code == "pack_whatsapp_exhausted"
        # Renewing early adds to the same window, so there is room again
        tenant.pack_state = {"whatsapp": {**entry, "whatsapp_messages": 1003}}
        await db.commit()
        await plans.ensure_whatsapp_available(db, tenant.id)


async def test_the_email_pack_counts_emails_and_no_pack_includes_sms() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db, "packs")
        entry = _entry(20, email_messages=2)
        tenant.pack_state = {"email_sms": entry}
        when = datetime.fromisoformat(entry["start"]) + timedelta(hours=1)
        await _event(db, tenant.id, "email", when, 2)
        await db.commit()
        with pytest.raises(plans.PlanLimitError) as err:
            await plans.ensure_email_available(db, tenant.id)
        assert err.value.code == "pack_email_exhausted"
        # Holding the Email pack does not buy texts: SMS belongs to pay as you go.
        with pytest.raises(plans.PlanLimitError) as err:
            await plans.ensure_sms_available(db, tenant.id)
        assert err.value.code == "sms_not_in_packs" and "pay as you go" in str(err.value)


def test_the_email_pack_is_email_only() -> None:
    pack = packs.CATALOGUE["email_sms"]
    assert pack.name == "Email pack" and pack.allowances == {"email_messages": 5000}
    assert "sms" not in pack.blurb.lower()


async def test_call_minutes_are_the_packs_minutes_and_an_expired_pack_allows_none(monkeypatch: pytest.MonkeyPatch) -> None:
    used = {"minutes": 240}

    async def minutes_between(db, tenant_id, start, end):
        return used["minutes"]

    monkeypatch.setattr(plans, "billable_minutes_between", minutes_between)

    async def own_ai(db, tenant_id):
        return True

    monkeypatch.setattr(plans, "_included_ai", own_ai)
    async with session_scope() as db:
        tenant = await _free_tenant(db, "packs")
        tenant.pack_state = {"call": _entry(20, ai_minutes=250)}
        await db.commit()
        assert await plans.minutes_remaining(db, tenant) == 10
        await plans.ensure_minutes_available(db, tenant.id)
        used["minutes"] = 250
        with pytest.raises(plans.PlanLimitError) as err:
            await plans.ensure_minutes_available(db, tenant.id)
        assert err.value.code == "pack_minutes_exhausted"
        tenant.pack_state = {"call": _entry(-1, ai_minutes=250)}
        await db.commit()
        assert await plans.minutes_remaining(db, tenant) == 0


async def test_the_billing_snapshot_for_packs_shows_expiry_per_channel() -> None:
    async with session_scope() as db:
        tenant = await _free_tenant(db, "packs")
        tenant.pack_state = {"whatsapp": _entry(12, whatsapp_messages=1000)}
        await db.commit()
        snap = await plans.entitlements(db, tenant)
        by_key = {m["key"]: m for m in snap["meters"]}
        assert snap["tier"] == "packs"
        assert by_key["whatsapp_messages"]["limit"] == 1000 and by_key["whatsapp_messages"]["expires_at"]
        assert by_key["ai_minutes"]["limit"] == 0 and by_key["ai_minutes"]["expires_at"] is None


# ---- the API --------------------------------------------------------------------------------


async def _workspace(client: AsyncClient, slug: str, tier: str = "free") -> tuple[dict, uuid.UUID]:
    resp = await client.post("/api/v1/auth/signup", json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "Owner",
                             "email": f"o@{slug}.com", "password": "supersecret1"})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        tenant.plan_tier = tier
        owner = (await db.execute(select(User).where(User.email == f"o@{slug}.com"))).scalar_one()
        owner.email_verified_at = datetime.now(UTC)
        await db.commit()
        tenant_id = tenant.id
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}, tenant_id


@pytest.fixture
def razorpay(monkeypatch):
    orders: list[dict] = []

    async def create_order(*, amount_paise, receipt, notes=None):
        order = {"id": f"order_{uuid.uuid4().hex[:14]}", "amount": amount_paise, "receipt": receipt, "notes": notes}
        orders.append(order)
        return order

    monkeypatch.setattr(razorpay_client, "create_order", create_order)
    monkeypatch.setattr(razorpay_client, "_auth", lambda: ("rzp_test_key", SECRET))
    monkeypatch.setattr(get_settings(), "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(get_settings(), "seller_state_code", "27")
    return orders


def _sig(order_id: str, payment_id: str) -> str:
    return hmac.new(SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def _buy(client, auth, items, **extra):
    return client.post("/api/v1/packs/purchases", headers=auth,
                       json={"items": items, "billing_name": "Demo Pvt Ltd", "gstin": GSTIN_MH, **extra})


async def test_the_server_prices_the_order_and_a_client_cannot_change_the_amount(client: AsyncClient, razorpay) -> None:
    auth, _ = await _workspace(client, "pk-price")
    made = await _buy(client, auth, ["call"], amount_paise=1, price_paise=1, total_paise=1)
    assert made.status_code == 201, made.text
    body = made.json()
    gst = round(CALL * 0.18)
    assert body["price_paise"] == CALL and body["gst_paise"] == gst and body["amount_paise"] == CALL + gst
    assert razorpay[0]["amount"] == CALL + gst  # what was actually sent to the provider
    bundle = (await _buy(client, auth, ["call", "whatsapp", "email_sms"])).json()
    assert bundle["bundle"] is True and bundle["discount_paise"] == round((CALL + WA + MAIL) * 0.10)
    assert bundle["price_paise"] == CALL + WA + MAIL - bundle["discount_paise"]
    assert razorpay[1]["amount"] == bundle["amount_paise"]


async def test_nothing_is_granted_until_the_payment_is_proved(client: AsyncClient, razorpay) -> None:
    auth, tenant_id = await _workspace(client, "pk-proof")
    order = (await _buy(client, auth, ["whatsapp"])).json()
    url = f"/api/v1/packs/purchases/{order['purchase_id']}/verify"
    forged = await client.post(url, headers=auth, json={"payment_id": "pay_1234567", "signature": "0" * 64})
    assert forged.status_code == 400
    async with session_scope() as db:
        tenant = await db.get(Tenant, tenant_id)
        assert tenant.pack_state == {} and tenant.plan_tier == "free"
    good = {"payment_id": "pay_abcdef12", "signature": _sig(order["order_id"], "pay_abcdef12")}
    done = await client.post(url, headers=auth, json=good)
    assert done.status_code == 200 and done.json()["status"] == "paid" and set(done.json()["held"]) == {"whatsapp"}
    first_expiry = done.json()["held"]["whatsapp"]["expires_at"]
    again = await client.post(url, headers=auth, json=good)  # a double click or retry
    assert again.status_code == 200 and again.json()["held"]["whatsapp"]["expires_at"] == first_expiry
    mine = (await client.get("/api/v1/packs", headers=auth)).json()
    assert mine["on_packs"] is True and "whatsapp" in mine["held"]
    invoice = await client.get(f"/api/v1/packs/purchases/{order['purchase_id']}/invoice", headers=auth)
    assert invoice.status_code == 200 and "WhatsApp pack" in invoice.text and "CGST 9%" in invoice.text
    assert (await client.get(f"/api/v1/packs/purchases/{order['purchase_id']}/invoice", headers=auth)).status_code == 200


async def test_an_unpaid_order_has_no_invoice_and_other_workspaces_cannot_touch_it(client: AsyncClient, razorpay) -> None:
    auth, _ = await _workspace(client, "pk-own-a")
    other, _ = await _workspace(client, "pk-own-b")
    order = (await _buy(client, auth, ["call"])).json()
    pid = order["purchase_id"]
    assert (await client.get(f"/api/v1/packs/purchases/{pid}/invoice", headers=auth)).status_code == 404
    sig = {"payment_id": "pay_abcdef12", "signature": _sig(order["order_id"], "pay_abcdef12")}
    assert (await client.post(f"/api/v1/packs/purchases/{pid}/verify", headers=other, json=sig)).status_code == 404
    assert (await client.get(f"/api/v1/packs/purchases/{pid}/invoice", headers=other)).status_code == 404
    assert (await client.get("/api/v1/packs/purchases", headers=other)).json() == []
    assert (await client.get("/api/v1/packs/catalogue")).status_code == 401


async def test_only_workspaces_that_are_not_on_another_billing_model_can_buy(client: AsyncClient, razorpay) -> None:
    for slug, tier in (("pk-starter", "starter"), ("pk-payg", "payg"), ("pk-legacy", "legacy")):
        auth, _ = await _workspace(client, slug, tier)
        refused = await _buy(client, auth, ["call"])
        assert refused.status_code == 409, (tier, refused.text)
        assert (await client.get("/api/v1/packs/catalogue", headers=auth)).json()["can_buy"] is False
    assert razorpay == []  # no order was ever created for them


async def test_a_billing_name_is_needed_and_a_bad_gstin_is_refused(client: AsyncClient, razorpay) -> None:
    auth, _ = await _workspace(client, "pk-bill")
    nameless = await client.post("/api/v1/packs/purchases", headers=auth, json={"items": ["call"]})
    assert nameless.status_code == 422
    bad = await _buy(client, auth, ["call"], gstin="27AAPFU0939F1ZX")
    assert bad.status_code == 422
    mismatch = await _buy(client, auth, ["call"], state_code="29")
    assert mismatch.status_code == 422
    assert (await client.post("/api/v1/packs/purchases", headers=auth, json={"items": ["sms"], "billing_name": "Demo"})).status_code == 422


async def test_too_many_unpaid_orders_are_refused(client: AsyncClient, razorpay) -> None:
    auth, _ = await _workspace(client, "pk-spam")
    for _ in range(packs.MAX_OPEN_ORDERS_PER_HOUR):
        assert (await _buy(client, auth, ["call"])).status_code == 201
    assert (await _buy(client, auth, ["call"])).status_code == 429


def _webhook(client, payload: dict, secret: str = "test-razorpay-webhook-secret"):
    body = json.dumps(payload)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return client.post("/api/v1/webhooks/payment/razorpay", content=body,
                       headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})


async def test_the_webhook_applies_a_pack_when_the_browser_never_comes_back(client: AsyncClient, razorpay) -> None:
    auth, tenant_id = await _workspace(client, "pk-hook")
    order = (await _buy(client, auth, ["email_sms"])).json()
    event = lambda amount, order_id=order["order_id"]: {  # noqa: E731
        "event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_hook1234", "order_id": order_id, "amount": amount}}}}

    assert (await _webhook(client, event(order["amount_paise"] - 1))).status_code == 200  # wrong amount
    assert (await _webhook(client, event(order["amount_paise"], "order_someone_elses"))).status_code == 200
    forged = await _webhook(client, event(order["amount_paise"]), secret="not-the-secret")
    assert forged.status_code == 403
    async with session_scope() as db:
        assert (await db.get(Tenant, tenant_id)).pack_state == {}

    assert (await _webhook(client, event(order["amount_paise"]))).status_code == 200
    assert (await _webhook(client, event(order["amount_paise"]))).status_code == 200  # Razorpay retries
    async with session_scope() as db:
        tenant = await db.get(Tenant, tenant_id)
        assert set(packs.active(tenant)) == {"email_sms"} and tenant.plan_tier == "packs"
        rows = (await db.execute(select(PackPurchase))).scalars().all()
        assert [r.status for r in rows] == ["paid"] and rows[0].invoice_number


async def test_the_quote_endpoint_uses_the_same_prices_as_the_order(client: AsyncClient, razorpay) -> None:
    auth, _ = await _workspace(client, "pk-quote")
    one = (await client.get("/api/v1/packs/quote", headers=auth, params=[("items", "call")])).json()
    assert one["price_paise"] == CALL and one["bundle"] is False and one["total_paise"] == CALL + one["gst_paise"]
    allp = (await client.get("/api/v1/packs/quote", headers=auth,
                             params=[("items", "call"), ("items", "whatsapp"), ("items", "email_sms")])).json()
    assert allp["bundle"] is True and allp["discount_paise"] == round((CALL + WA + MAIL) * 0.10)
    order = (await _buy(client, auth, ["call", "whatsapp", "email_sms"])).json()
    assert order["price_paise"] == allp["price_paise"] and order["amount_paise"] == allp["total_paise"]
    assert (await client.get("/api/v1/packs/quote", headers=auth)).status_code == 422
    assert (await client.get("/api/v1/packs/quote", headers=auth, params=[("items", "sms")])).status_code == 422
