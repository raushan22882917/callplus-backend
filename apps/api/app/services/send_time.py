"""When to send. The AI part is learning from the workspace's own readers; the rest is arithmetic you can check.

Each open counts once toward its weekday and hour in the business's local time (a click counts three times, it is
a stronger sign someone is paying attention). With little data the answer leans on a sensible default for
business email (Tuesday to Thursday, mid-morning), and as opens come in the workspace's own pattern takes over.
Nothing is sent in the dead of night: only 8:00 to 20:59 local are offered.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
FIRST_HOUR, LAST_HOUR = 8, 20
LEARNED_AT = 60  # weighted opens at which the workspace's own pattern fully replaces the default
MIN_LEAD = timedelta(minutes=30)

_DAY_WEIGHT = (0.8, 1.0, 1.0, 1.0, 0.8, 0.3, 0.25)
_HOUR_WEIGHT = {8: 0.55, 9: 0.8, 10: 1.0, 11: 0.9, 12: 0.5, 13: 0.55, 14: 0.85, 15: 0.75, 16: 0.6, 17: 0.4, 18: 0.4, 19: 0.5, 20: 0.35}


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def empty_grid() -> list[list[float]]:
    return [[0.0] * 24 for _ in range(7)]


def add_event(grid: list[list[float]], when: datetime, tz: ZoneInfo, weight: float = 1.0) -> None:
    local = (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(tz)
    grid[local.weekday()][local.hour] += weight


def _prior() -> list[list[float]]:
    grid = [[(_DAY_WEIGHT[d] * _HOUR_WEIGHT.get(h, 0.0)) for h in range(24)] for d in range(7)]
    total = sum(sum(row) for row in grid) or 1.0
    return [[v / total for v in row] for row in grid]


def blended(grid: list[list[float]]) -> tuple[list[list[float]], float]:
    """The score for every weekday and hour, and how much of it is learned (0 to 1)."""
    seen = sum(sum(row) for row in grid)
    learned_share = min(1.0, seen / LEARNED_AT)
    prior = _prior()
    mix = [[(learned_share * (grid[d][h] / seen if seen else 0.0)) + ((1 - learned_share) * prior[d][h]) for h in range(24)] for d in range(7)]
    return mix, learned_share


def _reason(day: int, hour: int, grid: list[list[float]], learned_share: float) -> str:
    when = f"{DAYS[day]}s around {hour:02d}:00"
    if learned_share >= 0.999:
        return f"Your readers open most on {when}."
    if learned_share > 0:
        return f"Mostly the usual best time for business email, nudged by your own opens on {when}."
    return f"{when} is when business email is read most. This will sharpen as your readers open your emails."


def best_slots(
    grid: list[list[float]], tz_name: str | None, now: datetime | None = None, count: int = 3
) -> list[dict]:
    tz = zone(tz_name)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    mix, learned_share = blended(grid)
    earliest = now + MIN_LEAD
    candidates: list[tuple[float, datetime, int, int]] = []
    local_now = now.astimezone(tz)
    for offset in range(0, 8):
        day = (local_now + timedelta(days=offset)).date()
        for hour in range(FIRST_HOUR, LAST_HOUR + 1):
            local = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            at = local.astimezone(UTC)
            if at < earliest:
                continue
            weekday = local.weekday()
            candidates.append((mix[weekday][hour], at, weekday, hour))
    # Highest score first, then soonest. One slot per weekday so the choices are different days.
    candidates.sort(key=lambda c: (-round(c[0], 9), c[1]))
    top = candidates[0][0] if candidates else 1.0
    chosen: list[dict] = []
    used_days: set = set()
    for score, at, weekday, hour in candidates:
        key = at.astimezone(tz).date()
        if key in used_days:
            continue
        used_days.add(key)
        chosen.append(
            {
                "at": at.isoformat(),
                "local": f"{DAYS[weekday]} {hour:02d}:00",
                "score": round(100 * score / top) if top else 0,
                "reason": _reason(weekday, hour, grid, learned_share),
                "source": "learned" if learned_share >= 0.999 else ("mixed" if learned_share > 0 else "default"),
            }
        )
        if len(chosen) >= count:
            break
    return chosen
