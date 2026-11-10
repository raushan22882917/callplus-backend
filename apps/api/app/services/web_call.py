"""Voice calls from a web page: a visitor taps "Call us" and talks to the AI agent through the browser,
with no phone and no carrier. Phone calls reach the realtime gateway through Twilio or Exotel; a web call
reaches it straight from the visitor's browser, over a WebSocket, speaking the same Media Streams protocol.

The visitor never gets a call id they can pick: the API creates the Call row (so plan limits, billing,
transcripts and analysis all work as for any call) and hands back a short-lived signed address. The gateway
checks the signature (it shares `INTERNAL_SERVICE_TOKEN` with this service) before it will start a session.
"""

import hashlib
import hmac
import time

from app.core.config import get_settings

TTL_SECONDS = 300  # the address is only for connecting; the call itself can run longer


def _mac(call_id: str, exp: int) -> str:
    key = get_settings().internal_service_token.encode("utf-8")
    return hmac.new(key, f"web-call:{call_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def sign(call_id: str, *, now: float | None = None) -> tuple[int, str]:
    """(expiry as unix seconds, signature) for this call."""
    exp = int((time.time() if now is None else now) + TTL_SECONDS)
    return exp, _mac(call_id, exp)


def verify(call_id: str, exp: int, sig: str, *, now: float | None = None) -> bool:
    if (time.time() if now is None else now) > exp:
        return False
    return hmac.compare_digest(_mac(call_id, exp), sig)


def gateway_url(call_id: str) -> str:
    """The gateway's web-call endpoint, next to its Twilio one."""
    base = get_settings().realtime_ws_url
    base = base.rsplit("/ws/", 1)[0] if "/ws/" in base else base.rstrip("/")
    exp, sig = sign(call_id)
    return f"{base}/ws/web-call?call={call_id}&exp={exp}&sig={sig}"
