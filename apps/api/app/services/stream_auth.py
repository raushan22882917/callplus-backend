"""Proof that a carrier's audio stream belongs to a call this platform accepted.

The realtime gateway's carrier endpoint (`/ws/twilio`) is reachable by anyone who knows its address, and a
carrier cannot send a secret header with the stream. So the API signs the call id when it hands a call to
the carrier, the carrier passes the signature back with the stream's start message, and the gateway checks
it (it shares `INTERNAL_SERVICE_TOKEN` with this service) before it opens speech recognition. See
`apps/realtime/internal/session/streamauth.go` for the other side.
"""

import hashlib
import hmac
import time

from app.core.config import get_settings

# Long enough for an outbound call to ring and be answered. It gates the connection, not the call.
TTL_SECONDS = 600


def _mac(call_id: str, exp: int) -> str:
    key = get_settings().internal_service_token.encode("utf-8")
    return hmac.new(key, f"stream:{call_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def stream_params(call_id: str, *, now: float | None = None) -> dict[str, str]:
    """The extra stream parameters the gateway checks: an expiry (unix seconds) and its signature."""
    exp = int((time.time() if now is None else now) + TTL_SECONDS)
    return {"exp": str(exp), "sig": _mac(call_id, exp)}
