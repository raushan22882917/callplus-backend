""""STOP" typed back to us on WhatsApp, SMS or email is an opt-out from every channel.

Only the whole message counts (STOP, STOP ALL, UNSUBSCRIBE, QUIT, OPT OUT). "cancel" and "end" are left out
on purpose: a customer writing "cancel" usually means their appointment, not our marketing.
"""

import re

_STOP = {"stop", "stopall", "stop all", "unsubscribe", "quit", "optout", "opt out"}
OPT_OUT_REPLY = (
    "You are unsubscribed. We will not send you marketing messages on WhatsApp, SMS, email or calls. "
    "We can still reach you about things you asked for, like a booking."
)


def is_stop_message(text: str | None) -> bool:
    cleaned = re.sub(r"[^a-z ]", "", (text or "").lower().replace("-", "")).strip()
    return re.sub(r" +", " ", cleaned) in _STOP
