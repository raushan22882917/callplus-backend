"""Per-client-IP throttle for unauthenticated auth endpoints (login, signup,
password reset), reusing the Redis client of app.core.rate_limit.

Same fail-open policy as the API-key limiter: a Redis outage must not lock
every user out of the platform.
"""

import logging
import time

from app.core.config import get_settings
from app.core.ip_allowlist import client_ip
from app.core.rate_limit import WINDOW_SECONDS, _redis
from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def ip_rate_limit(bucket: str, *, limit: int):
    """Dependency factory: at most `limit` requests per minute per client IP."""

    async def dependency(request: Request) -> None:
        if limit <= 0 or not get_settings().auth_rate_limit_enabled:
            return
        now = int(time.time())
        key = f"rl:ip:{bucket}:{client_ip(request) or 'unknown'}:{now // WINDOW_SECONDS}"
        try:
            client = _redis()
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, WINDOW_SECONDS + 5)
        except (RedisError, OSError):
            logger.warning("ip rate limiter unavailable; allowing request", exc_info=True)
            return
        if count > limit:
            retry_after = WINDOW_SECONDS - (now % WINDOW_SECONDS)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many attempts. Please wait and try again.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency
