"""Signed-in sessions: one row per browser or device, referenced by every token.

Revoking a row signs that device out immediately (access tokens are checked on
every request, refresh tokens on every refresh). Tokens issued before this
existed carry no session id and keep working until they expire; the first
refresh gives them one.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.ip_allowlist import client_ip
from app.integrations.email_client import send_email
from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import User
from packages.database.models.session import UserSession

logger = logging.getLogger(__name__)

# How far back a matching device counts as "one you've used before".
KNOWN_DEVICE_WINDOW = timedelta(days=90)

_BROWSERS = (
    ("Edge", re.compile(r"Edg(e|A|iOS)?/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Chrome", re.compile(r"Chrome/|CriOS/")),
    ("Firefox", re.compile(r"Firefox/|FxiOS/")),
    ("Safari", re.compile(r"Safari/")),
)
_SYSTEMS = (
    ("iPhone", re.compile(r"iPhone")),
    ("iPad", re.compile(r"iPad")),
    ("Android", re.compile(r"Android")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("Linux", re.compile(r"Linux|X11")),
)


def describe_device(user_agent: str | None) -> str:
    """"Chrome on macOS" from a User-Agent; "Unknown device" if it says nothing."""
    if not user_agent:
        return "Unknown device"
    browser = next((name for name, rx in _BROWSERS if rx.search(user_agent)), None)
    system = next((name for name, rx in _SYSTEMS if rx.search(user_agent)), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"


async def create_session(
    db: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, request: Request | None
) -> UserSession:
    now = datetime.now(UTC)
    agent = request.headers.get("user-agent") if request is not None else None
    session = UserSession(
        user_id=user_id,
        tenant_id=tenant_id,
        user_agent=agent[:255] if agent else None,
        ip_address=client_ip(request) if request is not None else None,
        created_at=now,
        last_seen_at=now,
    )
    db.add(session)
    await db.flush()
    return session


async def is_new_device(
    db: AsyncSession, *, user_id: uuid.UUID, ip: str | None, device: str, exclude_id: uuid.UUID
) -> bool:
    """True when the user has signed in before, but never from this IP with
    this kind of device in the last 90 days. A user's very first session is not
    "new" (nothing to compare it with, and it would alert everyone once at rollout)."""
    since = datetime.now(UTC) - KNOWN_DEVICE_WINDOW
    earlier = (
        (
            await db.execute(
                select(UserSession).where(
                    UserSession.user_id == user_id,
                    UserSession.id != exclude_id,
                    UserSession.created_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    if not earlier:
        return False
    return not any(s.ip_address == ip and describe_device(s.user_agent) == device for s in earlier)


async def alert_new_sign_in(db: AsyncSession, user: User, session: UserSession) -> None:
    """Email the account owner when a sign-in comes from somewhere new. Best
    effort: it must never block or fail the sign-in itself."""
    try:
        device = describe_device(session.user_agent)
        if not await is_new_device(
            db, user_id=user.id, ip=session.ip_address, device=device, exclude_id=session.id
        ):
            return
        link = f"{get_settings().frontend_base_url.rstrip('/')}/settings/security"
        await send_email(
            to_email=user.email,
            subject="New sign-in to your account",
            html_body=(
                f"<p>Your account was just signed in to from <strong>{device}</strong>"
                f"{f' (IP {session.ip_address})' if session.ip_address else ''}.</p>"
                "<p>If this was you, there is nothing to do. If not, "
                f"<a href='{link}'>sign out that device and change your password</a>.</p>"
            ),
        )
    except Exception:
        logger.warning("new sign-in alert failed for user %s", user.id, exc_info=True)


async def session_is_active(db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    row = await db.get(UserSession, session_id)
    return row is not None and row.user_id == user_id and row.revoked_at is None


async def touch(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(UserSession).where(UserSession.id == session_id).values(last_seen_at=datetime.now(UTC))
    )


async def list_active(db: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
    rows = await db.execute(
        select(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_seen_at.desc())
    )
    return list(rows.scalars().all())


async def revoke(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    """Signs one of the user's own sessions out. False if it isn't theirs or is already gone."""
    row = await db.get(UserSession, session_id)
    if row is None or row.user_id != user_id or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(UTC)
    return True


async def revoke_others(db: AsyncSession, user_id: uuid.UUID, keep: uuid.UUID | None) -> int:
    rows = await list_active(db, user_id)
    now = datetime.now(UTC)
    count = 0
    for row in rows:
        if row.id != keep:
            row.revoked_at = now
            count += 1
    return count
