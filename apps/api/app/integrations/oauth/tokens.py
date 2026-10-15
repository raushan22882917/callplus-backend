"""Stores per-tenant OAuth connections and hands out always-fresh access
tokens. `get_valid_access_token` refreshes expiring tokens under a row lock
(SELECT ... FOR UPDATE in its own short transaction, so a refreshed/rotated
token is persisted even if the caller later rolls back) and records
`status` / `last_error` when refreshing fails."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from app.core.crypto import decrypt_secret, encrypt_secret
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import Identity, TokenResult, get_provider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.oauth import OAuthConnection, OAuthConnectionStatus
from packages.database.session import session_scope

logger = logging.getLogger(__name__)

REFRESH_SKEW = timedelta(seconds=120)
UNUSABLE = (OAuthConnectionStatus.EXPIRED.value, OAuthConnectionStatus.REVOKED.value)


async def get_connection(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str
) -> OAuthConnection | None:
    return (
        await db.execute(
            select(OAuthConnection).where(
                OAuthConnection.tenant_id == tenant_id, OAuthConnection.provider == provider
            )
        )
    ).scalar_one_or_none()


async def list_connections(db: AsyncSession, tenant_id: uuid.UUID) -> list[OAuthConnection]:
    return list(
        (
            await db.execute(
                select(OAuthConnection)
                .where(OAuthConnection.tenant_id == tenant_id)
                .order_by(OAuthConnection.provider)
            )
        )
        .scalars()
        .all()
    )


def _expiry(token: TokenResult, now: datetime) -> datetime | None:
    return now + timedelta(seconds=token.expires_in) if token.expires_in else None


async def upsert_connection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    provider: str,
    token: TokenResult,
    identity: Identity,
    requested_scopes: list[str],
    now: datetime | None = None,
) -> OAuthConnection:
    now = now or datetime.now(UTC)
    conn = await get_connection(db, tenant_id, provider)
    extra = {**token.extra, **identity.extra}
    if conn is None:
        conn = OAuthConnection(tenant_id=tenant_id, provider=provider, extra={})
        db.add(conn)
        previous_extra: dict[str, Any] = {}
        previous_refresh = None
    else:
        same_account = conn.external_account_id == identity.external_id
        previous_extra = conn.extra if same_account else {}
        previous_refresh = decrypt_secret(conn.encrypted_refresh_token) if (
            conn.encrypted_refresh_token and same_account
        ) else None
    refresh = token.refresh_token or previous_refresh
    conn.account_label = identity.label
    conn.account_email = identity.email
    conn.external_account_id = identity.external_id
    conn.scopes = token.scopes or requested_scopes
    conn.encrypted_access_token = encrypt_secret(token.access_token)
    conn.encrypted_refresh_token = encrypt_secret(refresh) if refresh else None
    conn.expires_at = _expiry(token, now)
    conn.last_refreshed_at = now
    conn.status = OAuthConnectionStatus.CONNECTED.value
    conn.last_error = None
    conn.connected_by_user_id = user_id
    conn.extra = {**previous_extra, **{k: v for k, v in extra.items() if v is not None}}
    await db.flush()
    return conn


def decrypt_access_token(conn: OAuthConnection) -> str:
    return decrypt_secret(conn.encrypted_access_token)


def _needs_refresh(conn: OAuthConnection, now: datetime) -> bool:
    return conn.expires_at is not None and conn.expires_at - REFRESH_SKEW <= now


async def get_valid_access_token(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    *,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> str:
    """Return a usable access token for the tenant's connection, refreshing
    it first when it expires within two minutes.

    Raises OAuthError: not_connected | reconnect_required (expired/revoked or
    refresh rejected) | refresh_failed (transient; connection marked `error`)."""
    now = now or datetime.now(UTC)
    conn = await get_connection(db, tenant_id, provider)
    if conn is None:
        raise OAuthError("not_connected", f"{provider} is not connected", status_code=404)
    if conn.status in UNUSABLE:
        raise OAuthError("reconnect_required", f"{provider} needs to be reconnected")
    if not _needs_refresh(conn, now):
        return decrypt_access_token(conn)
    return await _refresh_locked(tenant_id, provider, client=client, now=now)


async def _refresh_locked(
    tenant_id: uuid.UUID,
    provider_key: str,
    *,
    client: httpx.AsyncClient | None,
    now: datetime,
) -> str:
    provider = get_provider(provider_key)
    async with session_scope() as session:
        session.info["tenant_id"] = str(tenant_id)
        conn = (
            await session.execute(
                select(OAuthConnection)
                .where(
                    OAuthConnection.tenant_id == tenant_id, OAuthConnection.provider == provider_key
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if conn is None:
            raise OAuthError("not_connected", f"{provider_key} is not connected", status_code=404)
        if conn.status in UNUSABLE:
            raise OAuthError("reconnect_required", f"{provider_key} needs to be reconnected")
        # Another worker may have refreshed while we waited for the lock.
        if not _needs_refresh(conn, now):
            return decrypt_access_token(conn)

        secret = (
            decrypt_access_token(conn)
            if provider.refresh_from_access_token
            else (decrypt_secret(conn.encrypted_refresh_token) if conn.encrypted_refresh_token else None)
        )
        if secret is None:
            conn.status = OAuthConnectionStatus.EXPIRED.value
            conn.last_error = "Access token expired and no refresh token is stored"
            await session.commit()
            raise OAuthError("reconnect_required", f"{provider_key} needs to be reconnected")

        own_client = client is None
        http = client or new_client()
        try:
            try:
                result = await provider.refresh(http, refresh_token=secret, extra=dict(conn.extra))
            except OAuthError as exc:
                if exc.code == "refresh_rejected":
                    conn.status = OAuthConnectionStatus.EXPIRED.value
                    conn.last_error = exc.message[:500]
                    await session.commit()
                    raise OAuthError(
                        "reconnect_required", f"{provider_key} needs to be reconnected"
                    ) from exc
                if exc.code in ("refresh_failed",):
                    conn.status = OAuthConnectionStatus.ERROR.value
                    conn.last_error = exc.message[:500]
                    await session.commit()
                raise
        finally:
            if own_client:
                await http.aclose()

        conn.encrypted_access_token = encrypt_secret(result.access_token)
        if result.refresh_token and not provider.refresh_from_access_token:
            conn.encrypted_refresh_token = encrypt_secret(result.refresh_token)
        conn.expires_at = _expiry(result, now)
        conn.last_refreshed_at = now
        conn.status = OAuthConnectionStatus.CONNECTED.value
        conn.last_error = None
        if result.extra:
            conn.extra = {**conn.extra, **result.extra}
        await session.commit()
        return result.access_token


async def mark_status(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str, status: OAuthConnectionStatus, error: str | None
) -> None:
    conn = await get_connection(db, tenant_id, provider)
    if conn is not None:
        conn.status = status.value
        conn.last_error = error[:500] if error else None
        await db.flush()
