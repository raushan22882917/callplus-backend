"""Connect / callback / disconnect orchestration (provider-agnostic)."""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from app.core.crypto import decrypt_secret
from app.integrations.oauth import state as state_mod
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import get_provider, redirect_uri
from app.integrations.oauth.tokens import get_connection, upsert_connection
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.oauth import OAuthConnection

logger = logging.getLogger(__name__)


@dataclass
class AuthorizeResult:
    authorization_url: str
    expires_in: int


async def start_authorization(
    db: AsyncSession,
    *,
    provider_key: str,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    return_url: str | None = None,
    options: list[str] | None = None,
    shop: str | None = None,
) -> AuthorizeResult:
    provider = get_provider(provider_key)
    provider.require_configured()
    extra: dict[str, Any] = {}
    if provider.requires_shop:
        extra["shop"] = provider.normalize_shop(shop)  # type: ignore[attr-defined]
    scopes = provider.resolve_scopes(options)
    token, challenge, _ = await state_mod.create_state(
        db,
        provider=provider_key,
        purpose="connect",
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        return_url=return_url,
        extra={**extra, "scopes": scopes},
        use_pkce=provider.pkce,
    )
    url = provider.build_authorize_url(
        state=token, scopes=scopes, code_challenge=challenge, shop=extra.get("shop")
    )
    return AuthorizeResult(authorization_url=url, expires_in=int(state_mod.STATE_TTL.total_seconds()))


@dataclass
class CallbackResult:
    provider: str
    return_url: str
    connection: OAuthConnection


async def handle_callback(
    db: AsyncSession,
    *,
    provider_key: str,
    params: dict[str, str],
    client: httpx.AsyncClient | None = None,
) -> CallbackResult:
    """Validate state, exchange the code, persist the connection. Raises
    OAuthError (with .return_url attached when the state was readable)."""
    provider = get_provider(provider_key)
    return_url = state_mod.DEFAULT_RETURN_URL

    state = params.get("state")
    if not state:
        raise OAuthError("invalid_state", "Missing authorization state")
    # Peek at return_url (signature-checked) so errors land where the user started.
    try:
        return_url = state_mod.safe_return_url(state_mod.read_state(state).get("return_url"))
    except OAuthError:
        pass

    def fail(exc: OAuthError) -> OAuthError:
        exc.return_url = return_url  # type: ignore[attr-defined]
        return exc

    try:
        claims = await state_mod.consume_state(db, state, provider=provider_key, purpose="connect")
        return_url = claims.return_url
        if params.get("error"):
            denied = params["error"] == "access_denied"
            raise OAuthError(
                "access_denied" if denied else "provider_error",
                params.get("error_description") or params["error"],
            )
        code = params.get("code")
        if not code:
            raise OAuthError("invalid_callback", "The provider did not return an authorization code")
        if not claims.tenant_id or not claims.user_id:
            raise OAuthError("invalid_state", "Authorization state is incomplete")
        context = provider.validate_callback(params, claims.extra)
        own = client is None
        http = client or new_client()
        try:
            token = await provider.exchange_code(
                http, code=code, code_verifier=claims.code_verifier, context=context
            )
            try:
                identity = await provider.identity(http, token, context)
            except (OAuthError, httpx.HTTPError):
                logger.warning("identity lookup failed for %s", provider_key, exc_info=True)
                from app.integrations.oauth.providers import Identity

                identity = Identity(label=provider.label)
        finally:
            if own:
                await http.aclose()
        tenant_id = uuid.UUID(claims.tenant_id)
        conn = await upsert_connection(
            db,
            tenant_id=tenant_id,
            user_id=uuid.UUID(claims.user_id),
            provider=provider_key,
            token=token,
            identity=identity,
            requested_scopes=list(claims.extra.get("scopes") or provider.scopes),
        )
        await db.commit()
        await _after_connect(db, provider_key, conn)
        return CallbackResult(provider=provider_key, return_url=return_url, connection=conn)
    except OAuthError as exc:
        await db.rollback()
        raise fail(exc) from exc


async def _after_connect(db: AsyncSession, provider_key: str, conn: OAuthConnection) -> None:
    """Provider side effects that must never fail the connection."""
    if provider_key == "shopify":
        try:
            from app.services.platform_connectors import sync_shopify_connector

            await sync_shopify_connector(db, conn)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.warning("could not mirror shopify connection into connectors", exc_info=True)


async def disconnect(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider_key: str, client: httpx.AsyncClient | None = None
) -> bool:
    """Revoke at the provider when supported (best effort), then delete the
    row. Returns False when nothing was connected."""
    provider = get_provider(provider_key)
    conn = await get_connection(db, tenant_id, provider_key)
    if conn is None:
        return False
    access = decrypt_secret(conn.encrypted_access_token)
    refresh = decrypt_secret(conn.encrypted_refresh_token) if conn.encrypted_refresh_token else None
    extra = dict(conn.extra)
    if provider_key == "meta":
        from app.services import meta_channels

        await meta_channels.detach_all(db, tenant_id, access, client=client)
    if provider_key == "shopify":
        from app.services.platform_connectors import remove_shopify_connector

        await remove_shopify_connector(db, tenant_id)
    if provider.revoke_supported and provider.configured():
        own = client is None
        http = client or new_client()
        try:
            await provider.revoke(http, access_token=access, refresh_token=refresh, extra=extra)
        except Exception:
            logger.warning("provider-side revoke failed for %s", provider_key, exc_info=True)
        finally:
            if own:
                await http.aclose()
    await db.delete(conn)
    await db.commit()
    return True


def callback_uri(provider_key: str) -> str:
    return redirect_uri(provider_key)
