"""Call-volume forecast and staffing recommendation.

Forecast: simple moving average per (weekday, hour). For each of the 168
weekday-hour slots we average the call count over the last `window_weeks`
occurrences of that slot (each occurrence counts, including zero-call hours),
using the tenant's UTC offset to place calls in local time.

Staffing: Erlang C. With offered traffic A = calls_per_hour * AHT / 3600
(Erlangs) and N concurrent handlers (N > A):
    B(N)  = Erlang-B via recursion  B(0)=1, B(n) = A*B(n-1) / (n + A*B(n-1))
    C     = B / (1 - (A/N) * (1 - B))                  probability a call waits
    SL(N) = 1 - C * exp(-(N - A) * T / AHT)            share answered within T seconds
We return the smallest N with SL(N) >= target (default 80% in 20s) and
occupancy A/N <= max_occupancy (default 85%).
"""

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta


def erlang_c(traffic: float, agents: int) -> float:
    """Probability that an arriving call has to wait (0..1). 1.0 if agents <= traffic."""
    if traffic <= 0:
        return 0.0
    if agents <= traffic:
        return 1.0
    b = 1.0
    for n in range(1, agents + 1):
        b = traffic * b / (n + traffic * b)
    return b / (1 - (traffic / agents) * (1 - b))


def service_level(traffic: float, agents: int, aht_seconds: float, answer_seconds: float) -> float:
    if traffic <= 0:
        return 1.0
    if agents <= traffic:
        return 0.0
    c = erlang_c(traffic, agents)
    return max(0.0, 1 - c * math.exp(-(agents - traffic) * answer_seconds / aht_seconds))


def required_agents(
    calls_per_hour: float,
    aht_seconds: float,
    *,
    target_service_level: float = 0.8,
    answer_seconds: float = 20.0,
    max_occupancy: float = 0.85,
) -> dict:
    if calls_per_hour <= 0 or aht_seconds <= 0:
        return {"agents": 0, "traffic_erlangs": 0.0, "service_level": 1.0, "occupancy": 0.0}
    traffic = calls_per_hour * aht_seconds / 3600
    n = max(1, math.floor(traffic) + 1)
    while True:
        sl = service_level(traffic, n, aht_seconds, answer_seconds)
        if sl >= target_service_level and traffic / n <= max_occupancy:
            return {
                "agents": n,
                "traffic_erlangs": round(traffic, 3),
                "service_level": round(sl, 3),
                "occupancy": round(traffic / n, 3),
            }
        n += 1
        if n > 10_000:  # unreachable for sane input; guards against a runaway loop
            raise ValueError("staffing requirement exceeds supported size")


def weekday_hour_averages(
    call_times: list[datetime],
    *,
    now: datetime,
    window_weeks: int = 4,
    tz_offset_minutes: int = 0,
) -> dict[tuple[int, int], float]:
    """Average calls for each (weekday 0=Mon, hour) over the last `window_weeks` weeks."""
    offset = timedelta(minutes=tz_offset_minutes)
    start = now - timedelta(weeks=window_weeks)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for t in call_times:
        if start <= t <= now:
            local = t + offset
            counts[(local.weekday(), local.hour)] += 1
    return {
        (wd, hr): counts.get((wd, hr), 0) / window_weeks for wd in range(7) for hr in range(24)
    }


def forecast_days(
    call_times: list[datetime],
    *,
    now: datetime | None = None,
    days: int = 7,
    window_weeks: int = 4,
    tz_offset_minutes: int = 0,
) -> list[dict]:
    """Expected calls per hour for each of the next `days` local days."""
    now = now or datetime.now(UTC)
    avgs = weekday_hour_averages(
        call_times, now=now, window_weeks=window_weeks, tz_offset_minutes=tz_offset_minutes
    )
    local_today = (now + timedelta(minutes=tz_offset_minutes)).date()
    out = []
    for i in range(1, days + 1):
        d = local_today + timedelta(days=i)
        hours = [round(avgs[(d.weekday(), h)], 2) for h in range(24)]
        out.append({"date": d.isoformat(), "weekday": d.strftime("%A"), "hourly": hours, "total": round(sum(hours), 1)})
    return out


def staffing_plan(
    forecast: list[dict],
    aht_seconds: float,
    *,
    target_service_level: float = 0.8,
    answer_seconds: float = 20.0,
    max_occupancy: float = 0.85,
) -> list[dict]:
    plan = []
    for day in forecast:
        hourly = [
            required_agents(
                calls,
                aht_seconds,
                target_service_level=target_service_level,
                answer_seconds=answer_seconds,
                max_occupancy=max_occupancy,
            )["agents"]
            for calls in day["hourly"]
        ]
        plan.append({"date": day["date"], "weekday": day["weekday"], "hourly_agents": hourly, "peak_agents": max(hourly)})
    return plan
