"""Pure TwiML builders and IVR digit routing — no DB, no network. The webhook
layer (app/api/v1/telephony_plus.py) and the orchestration service
(app/services/telephony_plus.py) feed these resolved data.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from twilio.twiml.voice_response import Connect, Dial, Gather, VoiceResponse

IVR_ACTIONS = ("ai_agent", "queue", "ring_group", "forward", "voicemail", "submenu", "hangup")
QUEUE_STRATEGIES = ("round_robin", "longest_idle", "simultaneous")
RING_STRATEGIES = ("simultaneous", "sequential")
MAX_RING_MEMBERS = 10

DEFAULT_HOLD_MUSIC = "http://com.twilio.sounds.music.s3.amazonaws.com/MARKRTQ.mp3"


# --- IVR menu ---------------------------------------------------------------


def validate_options(options: dict) -> list[str]:
    """Returns human-readable problems (empty list = valid)."""
    problems: list[str] = []
    for digit, action in options.items():
        if len(digit) != 1 or digit not in "0123456789*#":
            problems.append(f"option key {digit!r} must be a single digit, * or #")
            continue
        if not isinstance(action, dict) or action.get("action") not in IVR_ACTIONS:
            problems.append(f"option {digit}: action must be one of {', '.join(IVR_ACTIONS)}")
            continue
        kind = action["action"]
        if kind in ("ai_agent", "queue", "ring_group", "submenu") and not action.get("target_id"):
            problems.append(f"option {digit}: {kind} requires target_id")
        if kind == "forward" and not action.get("number"):
            problems.append(f"option {digit}: forward requires number")
    return problems


def resolve_digit(options: dict, digits: str | None) -> dict | None:
    """The configured action for a pressed digit, or None if unmapped/empty."""
    if not digits:
        return None
    action = options.get(digits.strip()[:1])
    return action if isinstance(action, dict) else None


def build_menu_twiml(
    *,
    greeting: str,
    language: str,
    digit_url: str,
    timeout_seconds: int = 6,
) -> str:
    """<Gather> that speaks the greeting and posts the digit to digit_url. If the
    caller presses nothing, the <Redirect> re-posts (no Digits) so the digit
    handler can count the retry."""
    response = VoiceResponse()
    gather = Gather(
        input="dtmf",
        num_digits=1,
        timeout=timeout_seconds,
        action=digit_url,
        method="POST",
    )
    gather.say(greeting, language=language)
    response.append(gather)
    response.redirect(digit_url, method="POST")
    return str(response)


def build_say_hangup_twiml(message: str | None = None, language: str = "en-IN") -> str:
    response = VoiceResponse()
    if message:
        response.say(message, language=language)
    response.hangup()
    return str(response)


def build_stream_twiml(*, stream_url: str, call_id: str, tenant_id: str) -> str:
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=stream_url)
    stream.parameter(name="callId", value=call_id)
    stream.parameter(name="tenantId", value=tenant_id)
    response.append(connect)
    return str(response)


def build_forward_twiml(
    number: str,
    *,
    caller_id: str | None = None,
    timeout: int = 25,
    action_url: str | None = None,
    time_limit: int | None = None,
) -> str:
    response = VoiceResponse()
    kwargs = {"timeout": timeout}
    if caller_id:
        kwargs["caller_id"] = caller_id
    if action_url:
        kwargs["action"] = action_url
        kwargs["method"] = "POST"
    if time_limit:
        kwargs["time_limit"] = time_limit
    dial = Dial(**kwargs)
    dial.number(number)
    response.append(dial)
    return str(response)


def build_ring_group_twiml(
    members: list[str],
    *,
    strategy: str,
    timeout: int,
    caller_id: str | None = None,
    next_url: str | None = None,
    index: int = 0,
    time_limit: int | None = None,
) -> str:
    """simultaneous: <Dial> every member at once. sequential: dial members[index]
    only; the <Dial action> (next_url) re-enters with index+1 until exhausted."""
    members = members[:MAX_RING_MEMBERS]
    response = VoiceResponse()
    if not members or index >= len(members):
        response.hangup()
        return str(response)
    kwargs: dict = {"timeout": timeout}
    if caller_id:
        kwargs["caller_id"] = caller_id
    if time_limit:
        kwargs["time_limit"] = time_limit
    if strategy == "sequential":
        if next_url:
            kwargs["action"] = f"{next_url}{'&' if '?' in next_url else '?'}idx={index + 1}"
            kwargs["method"] = "POST"
        dial = Dial(**kwargs)
        dial.number(members[index])
    else:
        if next_url:
            kwargs["action"] = next_url
            kwargs["method"] = "POST"
        dial = Dial(**kwargs)
        for member in members:
            dial.number(member)
    response.append(dial)
    return str(response)


def build_voicemail_twiml(
    *, message: str, language: str, recording_callback_url: str, max_length: int = 120
) -> str:
    response = VoiceResponse()
    response.say(message, language=language)
    response.record(
        max_length=max_length,
        play_beep=True,
        recording_status_callback=recording_callback_url,
        recording_status_callback_method="POST",
        recording_status_callback_event="completed",
    )
    response.hangup()
    return str(response)


# --- queues -----------------------------------------------------------------


def twilio_queue_name(tenant_id: str, queue_id: str) -> str:
    """Twilio queue names are account-global, so scope by tenant + queue id."""
    return f"{tenant_id[:8]}-{queue_id}"


def build_enqueue_twiml(*, queue_name: str, wait_url: str, result_url: str) -> str:
    response = VoiceResponse()
    response.enqueue(queue_name, wait_url=wait_url, wait_url_method="POST", action=result_url)
    return str(response)


def build_queue_wait_twiml(
    *,
    position: int | None,
    hold_message: str,
    music_url: str | None,
    queue_time_seconds: int,
    max_wait_seconds: int,
    language: str = "en-IN",
) -> str:
    """Returned for each <Enqueue waitUrl> poll: announce position + hold
    message and play music; <Leave/> once the caller has waited too long."""
    response = VoiceResponse()
    if queue_time_seconds >= max_wait_seconds:
        response.say(
            "Everyone is still busy on other calls. Please try again in a few minutes.",
            language=language,
        )
        response.leave()
        return str(response)
    if position:
        response.say(
            f"All our people are on other calls. You are number {position}.",
            language=language,
        )
    if hold_message:
        response.say(hold_message, language=language)
    response.play(music_url or DEFAULT_HOLD_MUSIC)
    return str(response)


def build_queue_agent_twiml(queue_name: str, *, time_limit: int | None = None) -> str:
    """TwiML for an agent leg: connect to the longest-waiting caller."""
    response = VoiceResponse()
    dial = Dial(**({"time_limit": time_limit} if time_limit else {}))
    dial.queue(queue_name)
    response.append(dial)
    return str(response)


# --- masking / parking ------------------------------------------------------


def build_masked_bridge_twiml(*, customer_number: str, caller_id: str) -> str:
    """Played on the agent's answered leg: dials the customer presenting the
    tenant's number, so neither side sees the other's real number."""
    response = VoiceResponse()
    dial = Dial(caller_id=caller_id, answer_on_bridge=True)
    dial.number(customer_number)
    response.append(dial)
    return str(response)


def build_park_twiml(*, conference_name: str, wait_url: str) -> str:
    """Parks the caller alone in a conference with hold music until retrieved."""
    response = VoiceResponse()
    dial = Dial()
    dial.conference(
        conference_name,
        start_conference_on_enter=False,
        end_conference_on_exit=True,
        wait_url=wait_url,
        wait_method="POST",
        beep=False,
    )
    response.append(dial)
    return str(response)


def build_join_conference_twiml(conference_name: str) -> str:
    response = VoiceResponse()
    dial = Dial()
    dial.conference(
        conference_name, start_conference_on_enter=True, end_conference_on_exit=True, beep=False
    )
    response.append(dial)
    return str(response)


def build_hold_music_twiml(music_url: str | None = None) -> str:
    response = VoiceResponse()
    response.play(music_url or DEFAULT_HOLD_MUSIC)
    return str(response)


# --- business hours ---------------------------------------------------------


def is_open_now(
    now: datetime,
    *,
    tz_name: str,
    windows: list[dict],
    holiday_days: set | None = None,
) -> bool:
    """windows: [{"weekday": 0-6 (Mon=0), "start": "HH:MM", "end": "HH:MM"}].
    An empty windows list means "no hours configured" -> open (callers decide
    whether to call this at all)."""
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - unknown tz -> fail open rather than drop calls
        return True
    local = now.astimezone(zone)
    if holiday_days and local.date() in holiday_days:
        return False
    if not windows:
        return True
    for window in windows:
        if int(window.get("weekday", -1)) != local.weekday():
            continue
        try:
            start_h, start_m = (int(p) for p in str(window["start"]).split(":"))
            end_h, end_m = (int(p) for p in str(window["end"]).split(":"))
        except (KeyError, ValueError):
            continue
        if time(start_h, start_m) <= local.time().replace(second=0, microsecond=0) < time(
            end_h, end_m
        ):
            return True
    return False
