"""2FA persistence helpers shared by the login flow (auth.py) and the
/platform/security/2fa endpoints."""

import uuid
from datetime import UTC, datetime

from app.core.crypto import decrypt_secret
from app.core.rate_limit import _redis
from app.core.totp import hash_recovery_code, verify_totp
from fastapi import HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.platform import UserTotp


async def get_totp(db: AsyncSession, user_id: uuid.UUID) -> UserTotp | None:
    return (
        await db.execute(select(UserTotp).where(UserTotp.user_id == user_id))
    ).scalar_one_or_none()


async def get_enabled_totp(db: AsyncSession, user_id: uuid.UUID) -> UserTotp | None:
    row = await get_totp(db, user_id)
    return row if row is not None and row.enabled else None


def check_code(row: UserTotp, code: str, *, now: float | None = None) -> bool:
    """Validates a TOTP code (rejecting replays) or a single-use recovery
    code, mutating `row`; the caller commits."""
    step = verify_totp(
        decrypt_secret(row.encrypted_secret), code, now=now, min_step=row.last_used_step
    )
    if step is not None:
        row.last_used_step = step
        return True
    digest = hash_recovery_code(code)
    hashes = list(row.recovery_code_hashes or [])
    if digest in hashes:
        hashes.remove(digest)
        row.recovery_code_hashes = hashes
        return True
    return False


MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SECONDS = 600


async def _failures(user_id: uuid.UUID, *, bump: bool) -> int:
    """Failed-2FA counter in Redis; fails open if Redis is down (as the API
    key rate limiter does)."""
    key = f"tfa:fail:{user_id}"
    try:
        client = _redis()
        if bump:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, FAILURE_WINDOW_SECONDS)
            return int(count)
        return int(await client.get(key) or 0)
    except (RedisError, OSError):
        return 0


async def _clear_failures(user_id: uuid.UUID) -> None:
    try:
        await _redis().delete(f"tfa:fail:{user_id}")
    except (RedisError, OSError):
        pass


async def verify_second_factor(db: AsyncSession, row: UserTotp, code: str) -> bool:
    """Checks a login/step-up code with brute-force throttling; commits the
    replay/recovery-code bookkeeping on success."""
    if await _failures(row.user_id, bump=False) >= MAX_FAILED_ATTEMPTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many invalid codes. Try again in a few minutes.",
        )
    ok = check_code(row, code)
    if ok:
        await db.commit()
        await _clear_failures(row.user_id)
    else:
        await _failures(row.user_id, bump=True)
    return ok


def mark_confirmed(row: UserTotp) -> None:
    row.enabled = True
    row.confirmed_at = datetime.now(UTC)
