"""Razorpay client for creating customer-facing payment links (a business
tool an AI agent/staff uses to collect payment — not platform billing) and,
separately, platform Subscriptions used for tenant billing (app/api/v1/billing.py).

Auth and the webhook/checkout signature formulas below (HMAC-SHA256, exact
string layouts) are confirmed against Razorpay's docs. The Payment Links
endpoint/body shape (`POST /v1/payment_links`) follows Razorpay's long-
standing public Payment Links API but was NOT independently re-verified
against live docs in this pass — confirm the field list against
razorpay.com/docs/api/payments/payment-links before relying on it with a
live key. The Subscriptions functions below follow Razorpay's public
Subscriptions API (`POST/GET /v1/subscriptions`, `POST .../cancel`) but were
likewise not independently re-verified against live docs — a Subscription
can only be created against a Plan that already exists on Razorpay's side
(see Plan.provider_plan_id in packages/database/models/billing.py); confirm
the exact field list against razorpay.com/docs/api/payments/subscriptions
before relying on it with a live key.
"""

import hashlib
import hmac

import httpx
from app.core.config import get_settings

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"


class RazorpayAPIError(Exception):
    pass


def _auth() -> tuple[str, str]:
    settings = get_settings()
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise RazorpayAPIError("RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are not configured")
    return settings.razorpay_key_id, settings.razorpay_key_secret


async def create_payment_link(
    *,
    amount_paise: int,
    currency: str = "INR",
    description: str,
    customer_name: str | None = None,
    customer_contact: str | None = None,
) -> dict:
    payload: dict = {
        "amount": amount_paise,
        "currency": currency,
        "description": description,
        "notify": {"sms": bool(customer_contact), "email": False},
        "reminder_enable": True,
    }
    if customer_name or customer_contact:
        payload["customer"] = {"name": customer_name, "contact": customer_contact}

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{RAZORPAY_API_BASE}/payment_links", auth=_auth(), json=payload
        )
    if response.status_code >= 400:
        raise RazorpayAPIError(
            f"Razorpay payment link failed ({response.status_code}): {response.text}"
        )
    return response.json()


async def create_subscription(
    *, plan_id: str, total_count: int, customer_notify: bool = True
) -> dict:
    """Create a Razorpay Subscription for a pre-existing Razorpay Plan
    (`plan_id` here is Razorpay's plan id, i.e. Plan.provider_plan_id — not
    our own Plan.id). `total_count` is the number of billing cycles Razorpay
    will run before the subscription completes (e.g. 12 for a monthly plan
    billed for a year); pass a large number for an effectively indefinite
    subscription. The frontend completes checkout by opening Razorpay
    Checkout with the returned subscription id — there is no short_url like
    payment links have.
    """
    payload = {
        "plan_id": plan_id,
        "total_count": total_count,
        "customer_notify": 1 if customer_notify else 0,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{RAZORPAY_API_BASE}/subscriptions", auth=_auth(), json=payload
        )
    if response.status_code >= 400:
        raise RazorpayAPIError(
            f"Razorpay subscription creation failed ({response.status_code}): {response.text}"
        )
    return response.json()


async def fetch_subscription(subscription_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{RAZORPAY_API_BASE}/subscriptions/{subscription_id}", auth=_auth()
        )
    if response.status_code >= 400:
        raise RazorpayAPIError(
            f"Razorpay subscription fetch failed ({response.status_code}): {response.text}"
        )
    return response.json()


async def cancel_subscription(subscription_id: str, *, cancel_at_cycle_end: bool = True) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{RAZORPAY_API_BASE}/subscriptions/{subscription_id}/cancel",
            auth=_auth(),
            json={"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0},
        )
    if response.status_code >= 400:
        raise RazorpayAPIError(
            f"Razorpay subscription cancellation failed ({response.status_code}): {response.text}"
        )
    return response.json()


async def create_order(*, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    """A one-time payment (a wallet top-up). The browser opens Razorpay Checkout with the
    returned order id; the paid amount is confirmed by verify_checkout_signature and by the
    `payment.captured` / `order.paid` webhook."""
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt[:40],
        "notes": notes or {},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"{RAZORPAY_API_BASE}/orders", auth=_auth(), json=payload)
    if response.status_code >= 400:
        raise RazorpayAPIError(f"Razorpay order creation failed ({response.status_code}): {response.text}")
    return response.json()


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    settings = get_settings()
    if not signature_header or not settings.razorpay_webhook_secret:
        return False  # an unset secret would make the signature forgeable by anyone
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def verify_checkout_signature(*, order_id: str, payment_id: str, signature: str) -> bool:
    _, key_secret = _auth()
    expected = hmac.new(
        key_secret.encode("utf-8"), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
