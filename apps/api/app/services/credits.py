"""One credit price list for India and the rest of the world.

1 credit is ₹1 (100 paise). The wallet stores paise; this module is the only place that
decides how many paise a call uses. India calls on our Twilio line keep the top-up rate.
A browser call or the workspace's own Exotel number pays the speech rate only. A call we
place through Twilio to a number outside India pays that speech rate plus one phone-line
rate. Every country outside India uses that same rate.
"""

from __future__ import annotations

import math
import re

# Carrier paise per minute on our Twilio line for any number outside India.
# Same rate for every country. Added on top of the speech rate.
ABROAD_CARRIER_PAISE = 1_500
_ABROAD_NAME = "another country"
_DIGITS = re.compile(r"\D")


# Ledger rows are grouped into these. The billing page charges only the ones that were used.
_MESSAGE_CHANNELS = {
    "whatsapp": "whatsapp",
    "whatsapp_messages": "whatsapp",
    "sms": "sms",
    "email": "email",
}


def service_key(kind: str, description: str, detail: dict | None = None) -> str | None:
    """Which bill line a ledger row belongs on. Top-ups and zero charges are not usage."""
    info = detail or {}
    if kind == "message":
        channel = str(info.get("channel") or "")
        if channel in _MESSAGE_CHANNELS:
            return _MESSAGE_CHANNELS[channel]
        text = description.lower()
        if text.startswith("whatsapp"):
            return "whatsapp"
        if text.startswith("sms"):
            return "sms"
        if text.startswith("email"):
            return "email"
        return None
    if kind != "call":
        return None
    text = description
    if text.startswith("Call to"):
        return "international"
    if text.startswith("Browser") or text.startswith("Own-line"):
        return "browser"
    if text.startswith("Inbound"):
        return "inbound"
    if text.startswith("Outbound"):
        return "outbound"
    direction = str(info.get("direction") or "")
    if direction in ("inbound", "outbound"):
        return direction
    return None


def usage_units(kind: str, detail: dict | None) -> int:
    """Minutes on a call, or messages on a send. A row with no count is one unit."""
    info = detail or {}
    raw = info.get("minutes") if kind == "call" else info.get("count")
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 1
    return count if count > 0 else 1


def credits_from_paise(paise: int) -> float:
    """How many credits a paise amount is. 1 credit = ₹1."""
    return paise / 100


def _digits(number: str | None) -> str:
    return _DIGITS.sub("", number or "")


def is_india(number: str | None) -> bool:
    """A missing number, a browser call, or +91 is India. Anything else is international."""
    digits = _digits(number)
    if not digits or number == "web":
        return True
    return digits.startswith("91")


def destination(number: str | None) -> tuple[str, int] | None:
    """(label, carrier paise per minute) for a number outside India, else None.

    Every country uses the same carrier rate.
    """
    if is_india(number):
        return None
    return _ABROAD_NAME, ABROAD_CARRIER_PAISE


def quote_call(
    *,
    direction: str,
    duration_seconds: int,
    cost_multiplier: float,
    from_number: str | None,
    to_number: str | None,
    exotel_numbers: set[str],
    inbound_rate: int,
    outbound_rate: int,
    own_line_paise: int,
) -> tuple[int, str]:
    """Paise to deduct for one finished call, and the ledger line the customer sees.

    A multiplier of 0 means the workspace pays the speech providers itself, so the
    speech part is free. We still charge the carrier when the call goes out on our
    Twilio line to another country, because that bill is ours.
    """
    minutes = max(1, math.ceil(duration_seconds / 60)) if duration_seconds > 0 else 0
    if minutes == 0:
        return 0, ""
    multiplier = float(cost_multiplier)
    own_line = from_number == "web" or bool(
        exotel_numbers and (from_number in exotel_numbers or to_number in exotel_numbers)
    )
    premium = f" x {multiplier:g} (premium voice)" if multiplier not in (0, 1) else ""

    if own_line:
        if multiplier == 0:
            kind = "Browser" if from_number == "web" else "Own-line"
            return 0, f"{kind} call, {minutes} min, own AI key, not charged"
        rate = round(own_line_paise * multiplier)
        kind = "Browser" if from_number == "web" else "Own-line"
        return minutes * rate, f"{kind} call, {minutes} min{premium}"

    abroad = destination(to_number) if direction == "outbound" else None
    if abroad is not None:
        name, carrier = abroad
        speech = 0 if multiplier == 0 else round(own_line_paise * multiplier)
        rate = speech + carrier
        return minutes * rate, f"Call to {name}, {minutes} min{premium}"

    if multiplier == 0:
        who = "Inbound" if direction == "inbound" else "Outbound"
        return 0, f"{who} call, {minutes} min, own AI key, not charged"
    rate = inbound_rate if direction == "inbound" else outbound_rate
    who = "Inbound" if direction == "inbound" else "Outbound"
    return round(minutes * rate * multiplier), f"{who} call, {minutes} min{premium}"


def price_card(
    *,
    inbound_paise: int,
    outbound_paise: int,
    own_line_paise: int,
    whatsapp_paise: int,
    sms_paise: int,
    email_paise: int,
) -> dict:
    """The list the billing page shows. The international row is speech plus the
    one phone-line rate, for an agent that uses our voice (multiplier 1)."""
    india = [
        {"id": "browser", "label": "Browser, or your own Exotel number", "paise": own_line_paise, "unit": "minute"},
        {"id": "inbound", "label": "Someone calls your number", "paise": inbound_paise, "unit": "minute"},
        {"id": "outbound", "label": "You call an Indian mobile", "paise": outbound_paise, "unit": "minute"},
    ]
    international = [
        {
            "id": "international",
            "label": "Calls outside India",
            "paise": own_line_paise + ABROAD_CARRIER_PAISE,
            "carrier_paise": ABROAD_CARRIER_PAISE,
            "unit": "minute",
        }
    ]
    messages = [
        {"id": "whatsapp", "label": "WhatsApp", "paise": whatsapp_paise, "unit": "message"},
        {"id": "sms", "label": "SMS", "paise": sms_paise, "unit": "message"},
        {"id": "email", "label": "Email", "paise": email_paise, "unit": "message"},
    ]
    return {"credit_paise": 100, "india": india, "international": international, "messages": messages}
