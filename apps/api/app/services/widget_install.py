"""Has the chat widget been loaded on a real website yet?

Every page that embeds the widget asks the server for its settings. Noting
which website that came from, and when, lets the setup wizard say "connected
on https://shop.example.com, 2 minutes ago" instead of leaving the owner to
guess. Kept in Redis (a short-lived hint, not business data); if Redis is down
the widget simply isn't reported as seen.
"""

import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

from app.core.rate_limit import _redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

MAX_SITES = 20
KEEP_SECONDS = 30 * 24 * 3600
# A site that was noted within this long ago isn't written again (page views are frequent).
REFRESH_SECONDS = 300


def _key(public_key: str) -> str:
    return f"wc:seen:{public_key}"


def site_of(origin: str | None, referer: str | None) -> str | None:
    """The website the widget was loaded on: the request's Origin, else the Referer's origin."""
    for candidate in (origin, referer):
        if not candidate or candidate == "null":
            continue
        parsed = urlparse(candidate)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"[:200]
    return None


async def note_seen(public_key: str, site: str | None, *, own_origin: str | None = None) -> None:
    """Best effort; never raises. The app's own pages (the designer preview, the test page) don't count."""
    if not site or site == own_origin:
        return
    try:
        client = _redis()
        key = _key(public_key)
        now = datetime.now(UTC)
        previous = await client.hget(key, site)
        if previous:
            try:
                if (now - datetime.fromisoformat(previous.decode() if isinstance(previous, bytes) else previous)).total_seconds() < REFRESH_SECONDS:
                    return
            except ValueError:
                pass
        await client.hset(key, site, now.isoformat())
        await client.expire(key, KEEP_SECONDS)
        if await client.hlen(key) > MAX_SITES:
            entries = await client.hgetall(key)
            oldest = sorted(entries.items(), key=lambda kv: kv[1])[: len(entries) - MAX_SITES]
            for field, _ in oldest:
                await client.hdel(key, field)
    except (RedisError, OSError):
        logger.warning("could not note widget load for %s", public_key[:6], exc_info=True)


async def seen_sites(public_key: str) -> list[dict]:
    """Websites that loaded the widget, newest first."""
    try:
        entries = await _redis().hgetall(_key(public_key))
    except (RedisError, OSError):
        return []
    out = []
    for site, at in entries.items():
        out.append({"site": site.decode() if isinstance(site, bytes) else site, "last_seen_at": at.decode() if isinstance(at, bytes) else at})
    return sorted(out, key=lambda s: s["last_seen_at"], reverse=True)
