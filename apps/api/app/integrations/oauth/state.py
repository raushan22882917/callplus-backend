"""Signed, expiring, single-use OAuth `state` plus PKCE.

The `state` query parameter is a JWT (HS256, keyed with the app's JWT secret)
binding tenant_id, user_id, provider, return_url and a random nonce. The
server-side `oauth_states` row makes it single use (atomic UPDATE ... WHERE
used_at IS NULL) and holds the PKCE code_verifier, which never leaves the
server. State is consumed *before* the code exchange so a replayed callback is
rejected even if the first attempt failed half-way."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.integrations.oauth.errors import OAuthError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.oauth import OAuthState

STATE_TYPE = "oauth_state"
STATE_TTL = timedelta(minutes=10)
DEFAULT_RETURN_URL = "/integrations/hub"


def generate_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, within the 43-128 limit
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def safe_return_url(value: str | None) -> str:
    """Only same-site relative paths: blocks open redirects through return_url."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_RETURN_URL
    if any(ch in value for ch in ("\r", "\n", "\t")) or "://" in value:
        return DEFAULT_RETURN_URL
    return value


@dataclass(frozen=True)
class StateClaims:
    provider: str
    purpose: str
    nonce: str
    tenant_id: str | None
    user_id: str | None
    return_url: str
    extra: dict[str, Any]
    code_verifier: str | None = None


def sign_state(
    *,
    provider: str,
    purpose: str,
    nonce: str,
    tenant_id: str | None,
    user_id: str | None,
    return_url: str,
    extra: dict[str, Any] | None = None,
    now: datetime | None = None,
    ttl: timedelta = STATE_TTL,
) -> str:
    settings = get_settings()
    now = now or datetime.now(UTC)
    return jwt.encode(
        {
            "typ": STATE_TYPE,
            "provider": provider,
            "purpose": purpose,
            "nonce": nonce,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "return_url": return_url,
            "extra": extra or {},
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def read_state(state: str) -> dict[str, Any]:
    """Verify signature + expiry. Raises OAuthError(invalid_state|state_expired)."""
    settings = get_settings()
    try:
        payload = jwt.decode(state, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise OAuthError("state_expired", "The authorization link expired; try again") from exc
    except jwt.PyJWTError as exc:
        raise OAuthError("invalid_state", "Invalid authorization state") from exc
    if payload.get("typ") != STATE_TYPE or not payload.get("nonce"):
        raise OAuthError("invalid_state", "Invalid authorization state")
    return payload


async def create_state(
    db: AsyncSession,
    *,
    provider: str,
    purpose: str,
    tenant_id: str | None,
    user_id: str | None,
    return_url: str | None,
    extra: dict[str, Any] | None = None,
    use_pkce: bool = False,
    now: datetime | None = None,
) -> tuple[str, str | None, str]:
    """Persist the server-side half and return (state_jwt, code_challenge, nonce).
    Commits so the row exists before the user is sent to the provider."""
    now = now or datetime.now(UTC)
    nonce = secrets.token_urlsafe(24)
    verifier = challenge = None
    if use_pkce:
        verifier, challenge = generate_pkce()
    # Opportunistic cleanup keeps the table small without a scheduled job.
    await db.execute(delete(OAuthState).where(OAuthState.expires_at < now - timedelta(hours=1)))
    db.add(
        OAuthState(
            nonce=nonce,
            provider=provider,
            purpose=purpose,
            encrypted_code_verifier=encrypt_secret(verifier) if verifier else None,
            expires_at=now + STATE_TTL,
        )
    )
    await db.commit()
    token = sign_state(
        provider=provider,
        purpose=purpose,
        nonce=nonce,
        tenant_id=tenant_id,
        user_id=user_id,
        return_url=safe_return_url(return_url),
        extra=extra,
        now=now,
    )
    return token, challenge, nonce


async def consume_state(
    db: AsyncSession, state: str, *, provider: str, purpose: str, now: datetime | None = None
) -> StateClaims:
    """Validate and burn a state. Replays fail with state_replayed."""
    payload = read_state(state)
    if payload.get("provider") != provider or payload.get("purpose") != purpose:
        raise OAuthError("invalid_state", "Authorization state does not match this provider")
    now = now or datetime.now(UTC)
    row = (
        await db.execute(
            update(OAuthState)
            .where(
                OAuthState.nonce == payload["nonce"],
                OAuthState.used_at.is_(None),
                OAuthState.expires_at > now,
            )
            .values(used_at=now)
            .returning(OAuthState.encrypted_code_verifier)
        )
    ).first()
    await db.commit()  # burn the state immediately, whatever happens next
    if row is None:
        existing = (
            await db.execute(select(OAuthState.used_at).where(OAuthState.nonce == payload["nonce"]))
        ).first()
        if existing is not None and existing[0] is not None:
            raise OAuthError("state_replayed", "This authorization link was already used")
        raise OAuthError("invalid_state", "Unknown or expired authorization state")
    verifier = decrypt_secret(row[0]) if row[0] else None
    return StateClaims(
        provider=payload["provider"],
        purpose=payload["purpose"],
        nonce=payload["nonce"],
        tenant_id=payload.get("tenant_id"),
        user_id=payload.get("user_id"),
        return_url=safe_return_url(payload.get("return_url")),
        extra=payload.get("extra") or {},
        code_verifier=verifier,
    )
