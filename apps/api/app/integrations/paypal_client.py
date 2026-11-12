"""PayPal Checkout orders for wallet top-ups and invoices.

The browser approves the order (the approve link). We capture it with the
client secret, then credit the wallet. The webhook does the same capture if
the browser never comes back.

The live account cannot receive INR (PayPal returns CURRENCY_NOT_SUPPORTED),
so the order is charged in US dollars. The rupee total converts at the live
USD to INR rate. The wallet still credits the rupee amount.
"""

import re
import time
from urllib.parse import urlencode

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
    """PayPal's decimal string ('118.00') as the minor unit (paise or cents)."""
    value = str((amount or {}).get("value") or "")
    if not _MONEY.match(value):
        return None
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def _inr_per_usd() -> int:
    rate = int(get_settings().paypal_inr_per_usd or 0)
    if rate <= 0:
        raise PayPalAPIError("PAYPAL_INR_PER_USD must be a positive number of rupees per dollar")
    return rate


# What an Indian PayPal account keeps from a dollar payment, from PayPal's own
# India seller fees (paypal.com/in/business/paypal-business-fees):
# 4.40% + $0.30 to receive it, 18% GST on that fee, and 3% off the rate when
# PayPal converts the dollars into rupees. The charge is the smallest number of
# cents that still leaves at least the rupee total after those three.
_FEE_KEEP = 94_808  # 1 - 4.40% * 1.18, scaled by 100_000
_FX_KEEP = 97  # 1 - 3%, scaled by 100
_FIXED_DRAG = 3_540_000  # $0.30 * 1.18, scaled by 100_000
_SCALE = 10_000_000  # 100_000 * 100, the scales above


def usd_cents(amount_paise: int, rate: int | None = None) -> int:
    """US cents to collect so the rupees that remain after PayPal's fee are at least `amount_paise`.

    `rate` is whole rupees per dollar. Cents are rounded up, never down.
    """
    used = rate if rate is not None else _inr_per_usd()
    if amount_paise <= 0 or used <= 0:
        raise PayPalAPIError("PayPal amount must be positive")
    numerator = amount_paise * _SCALE + _FIXED_DRAG * used * _FX_KEEP
    denominator = _FEE_KEEP * used * _FX_KEEP
    return max((numerator + denominator - 1) // denominator, 1)


_FX_URL = "https://open.er-api.com/v6/latest/USD"
_FX_TTL_SECONDS = 60
_fx_cache: tuple[int, float] | None = None


async def current_inr_per_usd(*, fresh: bool = False) -> int:
    """Live rupees per US dollar, kept for one minute.

    The rate is rounded down. A higher rate would collect fewer dollars than the
    rupee total is worth. A failed lookup uses the last rate, then PAYPAL_INR_PER_USD.
    `fresh` reads the rate again, which a new PayPal order does.
    """
    global _fx_cache
    now = time.time()
    if not fresh and _fx_cache and _fx_cache[1] > now:
        return _fx_cache[0]
    fetched = await _fetch_inr_per_usd()
    if fetched is not None:
        _fx_cache = (fetched, now + _FX_TTL_SECONDS)
        return fetched
    if _fx_cache:
        return _fx_cache[0]
    return _inr_per_usd()


async def _fetch_inr_per_usd() -> int | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(_FX_URL)
        if response.status_code >= 400:
            return None
        rate = float((response.json().get("rates") or {}).get("INR") or 0)
    except (httpx.HTTPError, TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    return int(rate)


def usd_value(amount_paise: int, rate: int | None = None) -> str:
    return _money(usd_cents(amount_paise, rate))


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def charge_cents(amount_paise: int, currency: str = "INR") -> int:
    """The US cents PayPal should collect.

    An INR amount is converted at the live rupee-per-dollar rate, with PayPal's
    fee covered so the rupees credited are not a loss. An amount already in USD
    is charged as those cents, with no second conversion.
    """
    code = (currency or "INR").upper()
    if amount_paise <= 0:
        raise PayPalAPIError("PayPal amount must be positive")
    if code == "USD":
        return amount_paise
    if code == "INR":
        return usd_cents(amount_paise)
    raise PayPalAPIError(f"PayPal charges US dollars. {code} is not set up.")


def amount_matches(
    amount: dict,
    amount_paise: int,
    *,
    expected_cents: int | None = None,
    currency: str = "INR",
) -> bool:
    """True when PayPal's money block is the dollars we asked that order to collect."""
    if (amount or {}).get("currency_code") != "USD":
        return False
    paid = paise_from_amount(amount)
    if paid is None:
        return False
    expected = expected_cents if expected_cents else charge_cents(amount_paise, currency)
    return paid == expected


def _order_usd_cents(order: dict) -> int | None:
    for unit in order.get("purchase_units") or []:
        amount = unit.get("amount") or {}
        if amount.get("currency_code") == "USD":
            return paise_from_amount(amount)
    return None


def capture_payment_id(
    order: dict,
    amount_paise: int,
    currency: str = "INR",
    expected_cents: int | None = None,
) -> str | None:
    """Capture id when PayPal collected the dollars on this order.

    `expected_cents` is the amount stored when the order was created. Without it,
    the capture is checked against the dollar amount on the order itself.
    """
    captured = capture_from_order(order)
    if captured is None:
        return None
    payment_id, paid_units, paid_currency = captured
    if paid_currency != "USD":
        return None
    if expected_cents:
        return payment_id if paid_units == expected_cents else None
    asked = _order_usd_cents(order)
    if asked is not None:
        return payment_id if paid_units == asked else None
    if paid_units != charge_cents(amount_paise, currency):
        return None
    return payment_id


def capture_from_order(order: dict) -> tuple[str, int, str] | None:
    """The completed capture on an order: (capture id, minor units, currency)."""
    if order.get("status") != "COMPLETED":
        return None
    for unit in order.get("purchase_units") or []:
        for capture in ((unit.get("payments") or {}).get("captures") or []):
            if capture.get("status") != "COMPLETED" or not capture.get("id"):
                continue
            paid = paise_from_amount(capture.get("amount") or {})
            paid_currency = (capture.get("amount") or {}).get("currency_code") or ""
            if paid is None:
                return None
            return capture["id"], paid, paid_currency
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


def checkout_urls(kind: str, **extra: str) -> tuple[str, str]:
    """Where PayPal sends the buyer after they pay or cancel. `kind` is `topup` or `invoice`."""
    base = get_settings().frontend_base_url.rstrip("/")
    return (
        f"{base}/billing?{urlencode({'paypal': kind, **extra})}",
        f"{base}/billing?paypal=cancel",
    )


def order_payload(
    *,
    amount_paise: int,
    receipt: str,
    custom_id: str,
    return_url: str | None = None,
    cancel_url: str | None = None,
    currency: str = "INR",
    rate: int | None = None,
) -> dict:
    """The PayPal order body. A return URL brings the buyer back to the dashboard."""
    code = (currency or "INR").upper()
    cents = amount_paise if code == "USD" else usd_cents(amount_paise, rate)
    value = _money(cents)
    description = f"CallPlus {value} USD" if code == "USD" else f"CallPlus {_rupees(amount_paise)} INR"
    payload: dict = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": receipt[:256],
                "custom_id": custom_id[:127],
                "invoice_id": receipt[:127],
                "description": description[:127],
                "amount": {"currency_code": "USD", "value": value},
            }
        ],
    }
    if return_url and cancel_url:
        payload["payment_source"] = {
            "paypal": {
                "experience_context": {
                    "brand_name": "CallPlus",
                    "shipping_preference": "NO_SHIPPING",
                    "user_action": "PAY_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                }
            }
        }
    return payload


async def create_order(
    *,
    amount_paise: int,
    receipt: str,
    custom_id: str,
    return_url: str | None = None,
    cancel_url: str | None = None,
    currency: str = "INR",
) -> dict:
    """An order the buyer approves. `id` is what we store; `approve_url` is the hosted page.

    `value` is the US-dollar charge PayPal is asked to collect. An INR total uses
    the live dollar rate; a USD total is charged as-is.
    When `return_url` is set, PayPal sends the buyer there with `token` set to this order id.
    """
    code = (currency or "INR").upper()
    if code not in ("INR", "USD"):
        raise PayPalAPIError(f"PayPal charges US dollars. {code} is not set up.")
    rate = await current_inr_per_usd(fresh=True) if code == "INR" else None
    token = await _access_token()
    payload = order_payload(
        amount_paise=amount_paise,
        receipt=receipt,
        custom_id=custom_id,
        return_url=return_url,
        cancel_url=cancel_url,
        currency=code,
        rate=rate,
    )
    value = payload["purchase_units"][0]["amount"]["value"]
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
        (
            link["href"]
            for link in body.get("links") or []
            if link.get("rel") in ("approve", "payer-action")
        ),
        None,
    )
    echoed = None
    units = body.get("purchase_units") or []
    if units:
        echoed = paise_from_amount(units[0].get("amount") or {})
    cents = echoed if echoed else paise_from_amount({"value": value})
    return {
        "id": body["id"],
        "approve_url": approve,
        "value": value,
        "currency": "USD",
        "charged_cents": cents,
        "fx_inr_per_usd": rate,
    }


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
