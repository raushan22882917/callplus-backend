"""PayPal Checkout orders for wallet top-ups.

The browser approves the order (JS SDK, or the approve link). We capture it
with the client secret, then credit the wallet. The webhook does the same
capture if the browser never comes back. Amounts are INR, matching the wallet.
"""

import re
import time

import httpx
from app.core.config import get_settings

_SANDBOX = "https://api-m.sandbox.paypal.com"
_LIVE = "https://api-m.paypal.com"
_CERT_PREFIXES = (
    "https://api.paypal.com/",
    "https://api-m.paypal.com/",
    "https://api.sandbox.paypal.com/",
    "https://api-m.sandbox.paypal.com/",
)
_MONEY = re.compile(r"^\d+(\.\d{1,2})?$")
_token_cache: dict[str, tuple[str, float]] = {}


class PayPalAPIError(Exception):
    pass


def _base() -> str:
    settings = get_settings()
    return _LIVE if settings.paypal_mode == "live" else _SANDBOX


def _credentials() -> tuple[str, str]:
    settings = get_settings()
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise PayPalAPIError("PAYPAL_CLIENT_ID/PAYPAL_CLIENT_SECRET are not configured")
    return settings.paypal_client_id, settings.paypal_client_secret


def paise_from_amount(amount: dict) -> int | None:
    """PayPal's decimal string ('118.00') as paise. Anything else is refused."""
    value = str((amount or {}).get("value") or "")
    if not _MONEY.match(value):
        return None
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def capture_from_order(order: dict) -> tuple[str, int, str] | None:
    """The completed capture on an order: (capture id, paise, currency)."""
    if order.get("status") != "COMPLETED":
        return None
    for unit in order.get("purchase_units") or []:
        for capture in ((unit.get("payments") or {}).get("captures") or []):
            if capture.get("status") != "COMPLETED" or not capture.get("id"):
                continue
            paid = paise_from_amount(capture.get("amount") or {})
            currency = (capture.get("amount") or {}).get("currency_code") or ""
            if paid is None:
                return None
            return capture["id"], paid, currency
    return None


async def _access_token() -> str:
    client_id, secret = _credentials()
    cached = _token_cache.get(client_id)
    if cached and cached[1] > time.time():
        return cached[0]
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_base()}/v1/oauth2/token",
            auth=(client_id, secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise PayPalAPIError(f"PayPal login failed ({response.status_code}): {response.text[:300]}")
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise PayPalAPIError("PayPal login did not return an access token")
    _token_cache[client_id] = (token, time.time() + max(int(body.get("expires_in") or 0) - 60, 30))
    return token


def _rupees(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


async def create_order(*, amount_paise: int, receipt: str, custom_id: str) -> dict:
    """An order the buyer approves. `id` is what we store; `approve_url` is the hosted page."""
    token = await _access_token()
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": receipt[:256],
                "custom_id": custom_id[:127],
                "invoice_id": receipt[:127],
                "amount": {"currency_code": "INR", "value": _rupees(amount_paise)},
            }
        ],
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{_base()}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code >= 400:
        raise PayPalAPIError(
            f"PayPal order failed ({response.status_code}): {response.text[:300]}"
        )
    body = response.json()
    approve = next(
        (link["href"] for link in body.get("links") or [] if link.get("rel") == "approve"),
        None,
    )
    return {"id": body["id"], "approve_url": approve}


async def capture_order(order_id: str) -> dict:
    """Captures an approved order. An order already captured is fetched instead."""
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{_base()}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
        )
    if response.status_code == 422 and "ORDER_ALREADY_CAPTURED" in response.text:
        return await get_order(order_id)
    if response.status_code >= 400:
        raise PayPalAPIError(
            f"PayPal capture failed ({response.status_code}): {response.text[:300]}"
        )
    return response.json()


async def get_order(order_id: str) -> dict:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_base()}/v2/checkout/orders/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        raise PayPalAPIError(
            f"PayPal order lookup failed ({response.status_code}): {response.text[:300]}"
        )
    return response.json()


async def verify_webhook(headers, event: dict) -> bool:
    """Asks PayPal whether this delivery is genuine. No webhook id means refuse."""
    settings = get_settings()
    transmission_id = headers.get("paypal-transmission-id")
    transmission_time = headers.get("paypal-transmission-time")
    transmission_sig = headers.get("paypal-transmission-sig")
    cert_url = headers.get("paypal-cert-url") or ""
    auth_algo = headers.get("paypal-auth-algo")
    signed = (
        transmission_id,
        transmission_time,
        transmission_sig,
        auth_algo,
        settings.paypal_webhook_id,
    )
    if not all(signed):
        return False
    if not any(cert_url.startswith(prefix) for prefix in _CERT_PREFIXES):
        return False
    token = await _access_token()
    payload = {
        "auth_algo": auth_algo,
        "cert_url": cert_url,
        "transmission_id": transmission_id,
        "transmission_sig": transmission_sig,
        "transmission_time": transmission_time,
        "webhook_id": settings.paypal_webhook_id,
        "webhook_event": event,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_base()}/v1/notifications/verify-webhook-signature",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code >= 400:
        return False
    return response.json().get("verification_status") == "SUCCESS"
