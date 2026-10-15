"""Verify the signature on webhooks sent by AutoNXT.

Each delivery carries `X-Timestamp` (unix seconds) and `X-Signature`, where
the signature is hex(HMAC-SHA256(signing_secret, f"{timestamp}.{raw_body}")).
Always verify against the RAW request body, before any JSON parsing."""

import hashlib
import hmac
import json
import time
from typing import Any


class SignatureVerificationError(Exception):
    pass


def compute_signature(secret: str, timestamp: int | str, body: str | bytes) -> str:
    raw = body.decode("utf-8") if isinstance(body, bytes) else body
    return hmac.new(secret.encode(), f"{timestamp}.{raw}".encode(), hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    timestamp: int | str,
    body: str | bytes,
    signature: str,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """True only if the signature matches and the timestamp is fresh."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if tolerance_seconds and abs(current - ts) > tolerance_seconds:
        return False
    expected = compute_signature(secret, ts, body)
    return hmac.compare_digest(expected, (signature or "").strip().lower())


def construct_event(
    secret: str,
    headers: dict[str, str],
    body: str | bytes,
    *,
    tolerance_seconds: int = 300,
) -> dict[str, Any]:
    """Verify then parse. Raises SignatureVerificationError on any problem."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if not verify_signature(
        secret,
        lowered.get("x-timestamp", ""),
        body,
        lowered.get("x-signature", ""),
        tolerance_seconds=tolerance_seconds,
    ):
        raise SignatureVerificationError("Invalid or expired webhook signature")
    return json.loads(body)
