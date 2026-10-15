"""Pure dialer scheduling logic (no I/O, no DB) — unit tested in
tests/test_telephony_plus.py. worker/campaign_tasks.py feeds it live numbers.

Modes
- preview:     an agent picks each contact manually; nothing is auto-dialed.
- progressive: one call per free agent (ratio 1.0), never overdials.
- power:       a fixed `pacing_ratio` calls per free agent.
- predictive:  ratio derived from the observed answer rate (1 / answer_rate),
               bounded by `max_ratio`, and backed off as the abandon rate
               approaches the cap.
"""

import math
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DIALER_MODES = ("preview", "progressive", "power", "predictive")
MIN_ANSWER_RATE = 0.05  # keeps predictive from exploding when answer rate ~ 0


def effective_ratio(
    *,
    mode: str,
    pacing_ratio: float,
    answer_rate: float,
    abandon_rate: float,
    abandon_rate_cap: float,
    max_ratio: float = 4.0,
) -> float:
    if mode == "predictive":
        ratio = min(1.0 / max(answer_rate, MIN_ANSWER_RATE), max_ratio)
    elif mode == "power":
        ratio = max(pacing_ratio, 1.0)
    else:  # progressive (and anything unknown: the conservative choice)
        return 1.0

    # Abandon-rate guard (regulators cap this, e.g. 3% of answered calls).
    if abandon_rate_cap > 0:
        if abandon_rate >= abandon_rate_cap:
            return 1.0
        if abandon_rate > 0.8 * abandon_rate_cap:
            ratio = 1.0 + (ratio - 1.0) * 0.5
    return max(ratio, 1.0)


def next_batch(
    *,
    mode: str,
    available_agents: int,
    in_flight: int,
    answer_rate: float,
    pacing_ratio: float = 1.0,
    max_concurrent: int = 1,
    abandon_rate: float = 0.0,
    abandon_rate_cap: float = 0.03,
    remaining_contacts: int = 0,
    daily_remaining: int | None = None,
    max_ratio: float = 4.0,
) -> int:
    """How many calls to place right now.

    available_agents: agents (or AI-agent slots) free to take a call.
    in_flight: calls already dialing/ringing that will claim an agent on answer.
    """
    if mode == "preview":
        return 0

    ratio = effective_ratio(
        mode=mode,
        pacing_ratio=pacing_ratio,
        answer_rate=answer_rate,
        abandon_rate=abandon_rate,
        abandon_rate_cap=abandon_rate_cap,
        max_ratio=max_ratio,
    )
    wanted_in_flight = math.floor(max(available_agents, 0) * ratio + 1e-9)
    batch = wanted_in_flight - max(in_flight, 0)
    batch = min(batch, max(max_concurrent, 0) - max(in_flight, 0), max(remaining_contacts, 0))
    if daily_remaining is not None:
        batch = min(batch, max(daily_remaining, 0))
    return max(batch, 0)


# --- time windows -----------------------------------------------------------


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def in_window(local: time, start: str, end: str) -> bool:
    """True if `local` is within [start, end). start > end means an overnight
    window (e.g. 21:00-09:00); start == end means the whole day."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s == e:
        return True
    if s < e:
        return s <= local < e
    return local >= s or local < e


def safe_zone(name: str | None, fallback: str = "UTC") -> ZoneInfo:
    try:
        return ZoneInfo(name or fallback)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(fallback)


# Calling-code -> representative zone. Longest prefix wins. +1 is refined by state.
_COUNTRY_ZONES = {
    "91": "Asia/Kolkata",
    "44": "Europe/London",
    "971": "Asia/Dubai",
    "966": "Asia/Riyadh",
    "65": "Asia/Singapore",
    "61": "Australia/Sydney",
    "49": "Europe/Berlin",
    "33": "Europe/Paris",
    "81": "Asia/Tokyo",
    "86": "Asia/Shanghai",
    "55": "America/Sao_Paulo",
    "27": "Africa/Johannesburg",
    "234": "Africa/Lagos",
    "254": "Africa/Nairobi",
    "52": "America/Mexico_City",
    "1": "America/New_York",
}

_US_STATE_ZONES = {
    **dict.fromkeys(
        "CT DE FL GA IN KY MA MD ME MI NC NH NJ NY OH PA RI SC VA VT WV DC".split(),
        "America/New_York",
    ),
    **dict.fromkeys("AL AR IA IL KS LA MN MO MS ND NE OK SD TN TX WI".split(), "America/Chicago"),
    **dict.fromkeys("AZ CO ID MT NM UT WY".split(), "America/Denver"),
    **dict.fromkeys("CA NV OR WA".split(), "America/Los_Angeles"),
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
}


def timezone_for_contact(
    phone: str | None,
    *,
    extra: dict | None = None,
    default: str = "Asia/Kolkata",
) -> str:
    """Per-contact zone: explicit `timezone` in the customer's extra data, else
    the US state (for +1), else the phone's country calling code, else default."""
    extra = extra or {}
    explicit = extra.get("timezone")
    if isinstance(explicit, str):
        try:
            ZoneInfo(explicit)
            return explicit
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass

    digits = (phone or "").strip()
    if digits.startswith("+"):
        digits = digits[1:]
        for length in (3, 2, 1):
            code = digits[:length]
            if code in _COUNTRY_ZONES:
                if code == "1":
                    state = str(extra.get("state", "")).strip().upper()
                    return _US_STATE_ZONES.get(state, _COUNTRY_ZONES["1"])
                return _COUNTRY_ZONES[code]
    return default


def local_time_for(now: datetime, tz_name: str) -> time:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(safe_zone(tz_name)).time().replace(second=0, microsecond=0)


def can_call_now(
    now: datetime,
    *,
    tz_name: str,
    window_start: str,
    window_end: str,
    quiet_start: str | None = None,
    quiet_end: str | None = None,
) -> bool:
    """Inside the campaign's call window AND outside quiet hours, both
    evaluated in the contact's own timezone."""
    local = local_time_for(now, tz_name)
    if not in_window(local, window_start, window_end):
        return False
    if quiet_start and quiet_end and quiet_start != quiet_end and in_window(
        local, quiet_start, quiet_end
    ):
        return False
    return True


def retry_delay_minutes(schedule: list[int], attempts_made: int) -> int:
    """Delay before the next attempt after `attempts_made` (>=1) attempts; the
    last schedule entry repeats. Empty schedule -> 60 minutes."""
    if not schedule:
        return 60
    index = min(max(attempts_made, 1) - 1, len(schedule) - 1)
    return max(int(schedule[index]), 1)


def answer_and_abandon_rates(
    *, attempts: int, answered: int, abandoned: int, default_answer_rate: float = 0.3
) -> tuple[float, float]:
    """(answer_rate, abandon_rate) from outcome counts. Abandon rate is
    abandoned / answered (the regulatory definition)."""
    answer_rate = answered / attempts if attempts > 0 else default_answer_rate
    abandon_rate = abandoned / answered if answered > 0 else 0.0
    return answer_rate, abandon_rate
