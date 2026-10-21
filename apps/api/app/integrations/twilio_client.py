"""Twilio REST client — outbound call origination, live-call redirect for
transfers, phone-number search/purchase, and TwiML building for Media
Streams. Protocol details (TwiML <Connect><Stream>, custom parameters, REST
Calls/AvailablePhoneNumbers/IncomingPhoneNumbers resources) are from
Twilio's docs; see app/api/v1/telephony.py for the webhook side.

Every function that calls Twilio's API takes an optional `credentials`
param (see app/integrations/credentials.py) — omit it to use the platform's
shared Twilio account, or pass a tenant's own (BYO) credentials. This is
what lets more than one tenant actually use the platform: without it, every
call would hit the same single Twilio account regardless of which tenant
it's for.
"""

from app.core.config import get_settings
from app.integrations.credentials import TwilioCredentials
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Dial, VoiceResponse


class TwilioClientError(Exception):
    pass


class CallNotLiveError(TwilioClientError):
    """The call already ended (the caller hung up, or it was never answered),
    so Twilio can no longer redirect or modify it."""


# 21220: "Call is not in-progress. Cannot redirect."  20404: the Call SID is gone.
_CALL_NOT_LIVE_CODES = {21220, 20404}


def redirect_live(*, client: Client, call_sid: str, twiml: str) -> None:
    """Replace a running call's TwiML, raising CallNotLiveError if it already ended."""
    try:
        client.calls(call_sid).update(twiml=twiml)
    except TwilioRestException as exc:
        if exc.code in _CALL_NOT_LIVE_CODES:
            raise CallNotLiveError(f"Call {call_sid} is no longer in progress") from exc
        raise


def _resolve(credentials: TwilioCredentials | None) -> TwilioCredentials:
    if credentials is not None:
        return credentials
    settings = get_settings()
    return TwilioCredentials(
        account_sid=settings.twilio_account_sid, auth_token=settings.twilio_auth_token
    )


def _client(credentials: TwilioCredentials | None = None) -> Client:
    creds = _resolve(credentials)
    if not creds.account_sid or not creds.auth_token:
        raise TwilioClientError("Twilio account_sid/auth_token are not configured")
    return Client(creds.account_sid, creds.auth_token)


def build_stream_twiml(*, call_id: str, tenant_id: str) -> str:
    settings = get_settings()
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=settings.realtime_ws_url)
    stream.parameter(name="callId", value=call_id)
    stream.parameter(name="tenantId", value=tenant_id)
    response.append(connect)
    return str(response)


MAX_RING_TARGETS = 5
TRANSFER_RING_TIMEOUT_SECONDS = 25
TRANSFER_UNAVAILABLE_MESSAGE = (
    "I'm sorry, nobody is available to take your call right now. "
    "We'll get back to you as soon as we can. Goodbye."
)


def build_transfer_twiml(numbers: list[str] | None = None) -> str:
    """TwiML for a transfer_call tool invocation. The handoff message was
    already spoken by the realtime gateway over the live media stream before
    the call is redirected (see app/api/v1/internal.py's /finalize), so
    there's no <Say> before the <Dial>.

    Rings every number in `numbers` simultaneously (first to answer wins),
    falling back to settings.transfer_fallback_number when the tenant has no
    transfer targets. If nobody answers, <Dial> falls through to the <Say>
    and <Hangup> below it. With no numbers at all there's no human to hand
    off to, so it just hangs up."""
    targets = list(numbers or [])
    if not targets and get_settings().transfer_fallback_number:
        targets = [get_settings().transfer_fallback_number]

    response = VoiceResponse()
    if not targets:
        response.hangup()
        return str(response)

    dial = Dial(timeout=TRANSFER_RING_TIMEOUT_SECONDS)
    for number in targets[:MAX_RING_TARGETS]:
        dial.number(number)
    response.append(dial)
    response.say(TRANSFER_UNAVAILABLE_MESSAGE)
    response.hangup()
    return str(response)


def build_hangup_twiml() -> str:
    """TwiML to end a call the platform is proactively terminating (max
    duration exceeded, no-response retries exhausted). Same no-<Say> reasoning
    as build_transfer_twiml — the closing message was already spoken live."""
    return str(VoiceResponse().hangup())


def place_outbound_call(
    *,
    to: str,
    from_: str,
    voice_webhook_url: str,
    status_callback_url: str | None = None,
    credentials: TwilioCredentials | None = None,
    amd_callback_url: str | None = None,
) -> str:
    """Returns the Twilio CallSid.

    amd_callback_url turns on asynchronous answering-machine detection: the
    call connects to the AI immediately and Twilio POSTs the verdict
    (AnsweredBy) to that URL a few seconds later, so humans hear no delay.

    status_callback_url is what tells us a call ended without ever being
    answered (no-answer/busy/failed) — those never reach voice_webhook_url
    (Twilio only requests that on answer) or the realtime gateway's Media
    Stream, so without this the call's DB row would stay "ringing" forever.
    Twilio's default StatusCallbackEvent ("completed") reports the call's
    final CallStatus regardless of what that status actually is, matching
    _TWILIO_STATUS_MAP in app/api/v1/telephony.py.
    """
    kwargs = {}
    if status_callback_url:
        kwargs["status_callback"] = status_callback_url
        kwargs["status_callback_method"] = "POST"
        # Twilio only reports "completed" unless asked, which left calls
        # showing "ringing" for their whole length.
        kwargs["status_callback_event"] = ["initiated", "ringing", "answered", "completed"]
    if amd_callback_url:
        kwargs["machine_detection"] = "DetectMessageEnd"
        kwargs["async_amd"] = "true"
        kwargs["async_amd_status_callback"] = amd_callback_url
        kwargs["async_amd_status_callback_method"] = "POST"
    call = _client(credentials).calls.create(
        to=to, from_=from_, url=voice_webhook_url, method="POST", **kwargs
    )
    return call.sid


def redirect_call(
    *, call_sid: str, twiml: str, credentials: TwilioCredentials | None = None
) -> None:
    """Used for a live human-transfer: replaces the running call's TwiML."""
    redirect_live(client=_client(credentials), call_sid=call_sid, twiml=twiml)


def start_call_recording(
    *,
    call_sid: str,
    status_callback_url: str,
    credentials: TwilioCredentials | None = None,
) -> None:
    """Starts a dual-channel recording on an already-live call. Twilio POSTs
    the finished recording's URL to status_callback_url (see the /recording
    webhook in app/api/v1/telephony.py)."""
    _client(credentials).calls(call_sid).recordings.create(
        recording_status_callback=status_callback_url,
        recording_status_callback_method="POST",
        recording_status_callback_event=["completed"],
        recording_channels="dual",
    )


def delete_recording(*, recording_sid: str, credentials: TwilioCredentials | None = None) -> bool:
    """Deletes the audio at Twilio. Returns False if it was already gone (404)."""
    try:
        _client(credentials).recordings(recording_sid).delete()
    except TwilioRestException as exc:
        if exc.status == 404:
            return False
        raise
    return True


def send_sms(
    *, to: str, from_: str, body: str, credentials: TwilioCredentials | None = None
) -> str:
    """Sends one SMS from a number the tenant owns; returns the Twilio MessageSid."""
    message = _client(credentials).messages.create(to=to, from_=from_, body=body)
    return message.sid


def validate_twilio_signature(
    *, url: str, params: dict, signature: str | None, auth_token: str | None = None
) -> bool:
    token = auth_token if auth_token is not None else get_settings().twilio_auth_token
    if not token or not signature:
        return False
    validator = RequestValidator(token)
    return validator.validate(url, params, signature)


def search_available_numbers(
    *,
    country: str,
    area_code: str | None = None,
    limit: int = 10,
    credentials: TwilioCredentials | None = None,
) -> list[dict]:
    """Numbers a tenant could provision — self-service, no Twilio console
    access needed. See POST /api/v1/phone-numbers/provision to actually buy one."""
    kwargs = {"limit": limit}
    if area_code:
        kwargs["area_code"] = area_code
    numbers = _client(credentials).available_phone_numbers(country).local.list(**kwargs)
    return [
        {
            "phone_number": n.phone_number,
            "friendly_name": n.friendly_name,
            "locality": n.locality,
            "region": n.region,
        }
        for n in numbers
    ]


def purchase_number(
    *, phone_number: str, voice_webhook_url: str, credentials: TwilioCredentials | None = None
) -> str:
    """Buys the number on the resolved Twilio account (platform's by default —
    see the module docstring) and points its voice webhook at us. Returns the
    Twilio PhoneNumberSid.

    This charges the Twilio account used (a real, recurring cost) — calling
    it is a purchase, not a preview; see search_available_numbers for that.
    """
    incoming = _client(credentials).incoming_phone_numbers.create(
        phone_number=phone_number, voice_url=voice_webhook_url, voice_method="POST"
    )
    return incoming.sid


def release_number(*, provider_sid: str, credentials: TwilioCredentials | None = None) -> None:
    """Releases a number back to Twilio (stops the recurring charge)."""
    _client(credentials).incoming_phone_numbers(provider_sid).delete()
