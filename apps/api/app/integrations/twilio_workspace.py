"""Twilio live-call control for the human-agent workspace: moving a call into a
Conference, dialling agents/supervisors into it, hold/mute, and fallback TwiML
(voicemail / callback) for transfers nobody can take. Credentials always come
from app/integrations/credentials.py (tenant's own or the platform's)."""

import asyncio
from collections.abc import Callable
from typing import Any

from app.integrations.credentials import TwilioCredentials
from app.integrations.twilio_client import TwilioClientError, _client
from twilio.base.exceptions import TwilioException
from twilio.twiml.voice_response import Dial, VoiceResponse

DEFAULT_VOICEMAIL_MESSAGE = (
    "Sorry, our team is not available right now. Please leave a message after the tone "
    "and we will get back to you."
)
DEFAULT_CALLBACK_MESSAGE = (
    "Sorry, our team is not available right now. We have noted your request and "
    "will call you back as soon as we can. Goodbye."
)


def require_credentials(credentials: TwilioCredentials) -> None:
    if not credentials.account_sid or not credentials.auth_token:
        raise TwilioClientError(
            "Twilio is not configured for this workspace. Connect Twilio under "
            "Integrations (or set the platform credentials) to control live calls."
        )


async def run_twilio(fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Runs a blocking Twilio SDK call off the event loop."""
    return await asyncio.to_thread(fn, **kwargs)


def build_voicemail_twiml(message: str | None) -> str:
    response = VoiceResponse()
    response.say(message or DEFAULT_VOICEMAIL_MESSAGE)
    response.record(max_length=120, play_beep=True)
    response.say("Thank you. Goodbye.")
    response.hangup()
    return str(response)


def build_callback_twiml(message: str | None) -> str:
    response = VoiceResponse()
    response.say(message or DEFAULT_CALLBACK_MESSAGE)
    response.hangup()
    return str(response)


def build_conference_twiml(
    name: str,
    *,
    start_on_enter: bool,
    end_on_exit: bool,
    muted: bool = False,
    coach: str | None = None,
) -> str:
    response = VoiceResponse()
    dial = Dial()
    kwargs: dict[str, Any] = {
        "start_conference_on_enter": start_on_enter,
        "end_conference_on_exit": end_on_exit,
        "muted": muted,
    }
    if coach:
        kwargs["coach"] = coach
    dial.conference(name, **kwargs)
    response.append(dial)
    return str(response)


def redirect_to_twiml(
    *, call_sid: str, twiml: str, credentials: TwilioCredentials | None = None
) -> None:
    _client(credentials).calls(call_sid).update(twiml=twiml)


def dial_leg(
    *,
    to: str,
    from_: str,
    twiml: str,
    status_callback_url: str | None = None,
    credentials: TwilioCredentials | None = None,
) -> str:
    """Places a call whose TwiML is `twiml`; returns the new leg's CallSid."""
    kwargs: dict[str, Any] = {}
    if status_callback_url:
        kwargs.update(
            status_callback=status_callback_url,
            status_callback_method="POST",
            status_callback_event=["completed"],
        )
    call = _client(credentials).calls.create(to=to, from_=from_, twiml=twiml, **kwargs)
    return call.sid


def hangup_leg(*, call_sid: str, credentials: TwilioCredentials | None = None) -> None:
    _client(credentials).calls(call_sid).update(status="completed")


def conference_sid(*, name: str, credentials: TwilioCredentials | None = None) -> str:
    found = _client(credentials).conferences.list(friendly_name=name, status="in-progress", limit=1)
    if not found:
        raise TwilioClientError(
            "The conference is not live yet — wait for the agent leg to answer."
        )
    return found[0].sid


def update_participant(
    *,
    conference_name: str,
    call_sid: str,
    hold: bool | None = None,
    muted: bool | None = None,
    credentials: TwilioCredentials | None = None,
) -> None:
    client = _client(credentials)
    sid = conference_sid(name=conference_name, credentials=credentials)
    kwargs: dict[str, Any] = {}
    if hold is not None:
        kwargs["hold"] = hold
    if muted is not None:
        kwargs["muted"] = muted
    client.conferences(sid).participants(call_sid).update(**kwargs)


__all__ = [
    "TwilioException",
    "TwilioClientError",
    "build_callback_twiml",
    "build_conference_twiml",
    "build_voicemail_twiml",
    "call_status",
    "conference_sid",
    "dial_leg",
    "hangup_leg",
    "redirect_to_twiml",
    "require_credentials",
    "run_twilio",
    "update_participant",
]


def call_status(*, call_sid: str, credentials: TwilioCredentials | None = None) -> str:
    return _client(credentials).calls(call_sid).fetch().status
