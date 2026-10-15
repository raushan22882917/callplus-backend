"""Per-API-key fixed-window rate limit backed by Redis.

Fails open: if Redis is unreachable the request goes through rather than
taking the whole public API down with the cache.
"""

import asyncio
import logging
import time
import uuid

from app.core.config import get_settings
from fastapi import HTTPException, status
from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


_client: Redis | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _redis() -> Redis:
    """One client per event loop: redis-py connections are bound to the loop
    that opened them, so a client reused from a different (e.g. test) loop
    fails with "Event loop is closed"."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
        _client_loop = loop
    return _client


async def enforce_rate_limit(api_key_id: uuid.UUID) -> None:
    limit = get_settings().api_key_rate_limit_per_minute
    if limit <= 0:
        return
    now = int(time.time())
    window = now // WINDOW_SECONDS
    key = f"rl:apikey:{api_key_id}:{window}"
    try:
        client = _redis()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, WINDOW_SECONDS + 5)
    except (RedisError, OSError):
        logger.warning("rate limiter unavailable; allowing request", exc_info=True)
        return
    if count > limit:
        retry_after = WINDOW_SECONDS - (now % WINDOW_SECONDS)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit exceeded ({limit} requests/minute). Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
