"""PII masking for transcripts and API responses: phone numbers, emails,
Aadhaar numbers and payment-card numbers."""

import re

_CARD_RE = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
# Aadhaar: 12 digits, first digit 2-9, optionally grouped 4-4-4.
_AADHAAR_RE = re.compile(r"(?<![\d])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d])")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{7,16}\d(?![\w])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask_phone(value: str | None) -> str | None:
    """Keep the leading '+' and the last 2 digits: +91*******21."""
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "*" * len(value)
    prefix = "+" if value.strip().startswith("+") else ""
    return f"{prefix}{'*' * (len(digits) - 2)}{digits[-2:]}"


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return value
    local, _, domain = value.rpartition("@")
    return f"{local[0]}{'*' * max(len(local) - 1, 2)}@{domain}"


def mask_aadhaar(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"XXXX XXXX {digits[-4:]}"


def mask_card(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_text(text: str | None) -> str | None:
    """Masks every card, Aadhaar, email and phone number found in free text."""
    if not text:
        return text

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return mask_card(m.group(0))
        return m.group(0)

    text = _CARD_RE.sub(card, text)
    text = _AADHAAR_RE.sub(lambda m: mask_aadhaar(m.group(0)), text)
    text = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)) or "", text)

    def phone(m: re.Match) -> str:
        if "*" in m.group(0):
            return m.group(0)
        digits = re.sub(r"\D", "", m.group(0))
        if 8 <= len(digits) <= 15:
            return mask_phone(m.group(0)) or ""
        return m.group(0)

    return _PHONE_RE.sub(phone, text)
