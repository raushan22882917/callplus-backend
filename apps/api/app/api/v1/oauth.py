"""Per-tenant OAuth endpoints: connect / reconnect / disconnect, resource
pickers and the public provider callback. Only the platform's OAuth app
credentials are global (settings); every tenant's tokens live encrypted in
`oauth_connections`."""

import logging
from urllib.parse import quote, urlencode

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_current_context, get_db
from app.core.exceptions import PermissionDeniedError
from app.core.permissions import Permissions
from app.integrations.oauth import flow, resources
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.providers import PROVIDERS, get_provider, redirect_uri
from app.integrations.oauth.state import DEFAULT_RETURN_URL
from app.integrations.oauth.tokens import get_connection, list_connections
from app.schemas.oauth import (
    AuthorizeRequest,
    AuthorizeResponse,
    OAuthProviderRead,
    ResourcesResponse,
    SelectRequest,
    TokenConnectRequest,
    WhatsAppSignupRequest,
    WhatsAppSignupResponse,
)
from app.services import connection_health, manual_token
from app.services import whatsapp_signup
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.oauth import OAuthConnection, OAuthConnectionStatus

router = APIRouter(prefix="/oauth", tags=["oauth"])
logger = logging.getLogger(__name__)

MANAGE_CODES = (Permissions.SETTINGS_MANAGE, Permissions.INTEGRATIONS_MANAGE)
READ_CODES = (Permissions.INTEGRATIONS_READ, *MANAGE_CODES)


def _require_any(*codes: str, allow_api_keys: bool = False):
    async def checker(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not allow_api_keys and ctx.api_key_id is not None:
            raise PermissionDeniedError("API keys cannot manage OAuth connections")
        if not any(ctx.has_permission(c) for c in codes):
            raise PermissionDeniedError(f"Missing permission: one of {', '.join(codes)}")
        return ctx

    return checker


can_manage = _require_any(*MANAGE_CODES)
can_read = _require_any(*READ_CODES, allow_api_keys=True)


def _http(exc: OAuthError) -> HTTPException:
    return HTTPException(exc.status_code, detail=exc.as_detail())


def _provider_or_404(key: str):
    try:
        return get_provider(key)
    except OAuthError as exc:
        raise _http(exc) from exc


def _read(provider, conn: OAuthConnection | None) -> OAuthProviderRead:
    needs_reconnect = bool(
        conn
        and (
            conn.status in (OAuthConnectionStatus.EXPIRED.value, OAuthConnectionStatus.REVOKED.value)
        )
    )
    return OAuthProviderRead(
        provider=provider.key,
        label=provider.label,
        category=provider.category,
        description=provider.description,
        configured=provider.configured(),
        connected=conn is not None,
        status=conn.status if conn else None,
        account_label=conn.account_label if conn else None,
        account_email=conn.account_email if conn else None,
        scopes=list(conn.scopes or []) if conn else [],
        expires_at=conn.expires_at if conn else None,
        auto_refresh=(
            conn is None
            or conn.expires_at is None
            or (conn.encrypted_refresh_token is not None and not provider.refresh_from_access_token)
        ),
        needs_reconnect=needs_reconnect,
        last_error=conn.last_error if conn else None,
        connected_at=conn.created_at if conn else None,
        features=list(provider.features),
        options=list(provider.scope_groups),
        requires_shop=provider.requires_shop,
        resources_kind=provider.resources_kind,
        selected=(conn.extra or {}).get("selected") if conn else None,
        redirect_uri=redirect_uri(provider.key),
        supports_login=provider.login,
        token_supported=provider.key in manual_token.SUPPORTED,
        token_label=manual_token.HELP[provider.key].label if provider.key in manual_token.HELP else None,
        token_steps=manual_token.HELP[provider.key].steps if provider.key in manual_token.HELP else None,
        token_url=manual_token.HELP[provider.key].url if provider.key in manual_token.HELP else None,
        token_placeholder=manual_token.HELP[provider.key].placeholder if provider.key in manual_token.HELP else None,
        via_token=bool(conn and (conn.extra or {}).get("manual")),
    )


@router.get("/providers", response_model=list[OAuthProviderRead])
async def list_providers(
    ctx: RequestContext = Depends(can_read), db: AsyncSession = Depends(get_db)
) -> list[OAuthProviderRead]:
    connections = {c.provider: c for c in await list_connections(db, ctx.tenant_id)}
    return [_read(p, connections.get(p.key)) for p in PROVIDERS.values()]


# --- WhatsApp Embedded Signup (declared before the /{provider} routes) -----------


@router.get("/whatsapp/config")
async def whatsapp_signup_config(_: RequestContext = Depends(can_manage)) -> dict:
    return whatsapp_signup.signup_config()


@router.post("/whatsapp/embedded-signup", response_model=WhatsAppSignupResponse)
async def whatsapp_embedded_signup(
    payload: WhatsAppSignupRequest,
    ctx: RequestContext = Depends(can_manage),
    db: AsyncSession = Depends(get_db),
) -> WhatsAppSignupResponse:
    try:
        account, pin = await whatsapp_signup.complete_signup(
            db,
            ctx.tenant_id,
            code=payload.code,
            phone_number_id=payload.phone_number_id,
            waba_id=payload.waba_id,
            pin=payload.pin,
        )
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action="oauth.whatsapp_connected",
            resource_type="whatsapp_account",
            resource_id=account.id,
        )
        await db.commit()
        connection_health.forget(ctx.tenant_id)
    except OAuthError as exc:
        await db.rollback()
        raise _http(exc) from exc
    return WhatsAppSignupResponse(
        id=str(account.id),
        phone_number_id=account.phone_number_id,
        waba_id=account.waba_id,
        display_phone_number=account.display_phone_number,
        registration_pin=pin,
    )


# --- connect flow ----------------------------------------------------------------


@router.post("/{provider}/authorize", response_model=AuthorizeResponse)
async def authorize(
    provider: str,
    payload: AuthorizeRequest | None = None,
    ctx: RequestContext = Depends(can_manage),
    db: AsyncSession = Depends(get_db),
) -> AuthorizeResponse:
    _provider_or_404(provider)
    payload = payload or AuthorizeRequest()
    try:
        result = await flow.start_authorization(
            db,
            provider_key=provider,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            return_url=payload.return_url,
            options=payload.options,
            shop=payload.shop,
        )
    except OAuthError as exc:
        raise _http(exc) from exc
    return AuthorizeResponse(authorization_url=result.authorization_url, expires_in=result.expires_in)


@router.post("/{provider}/token", status_code=201)
async def connect_with_token(
    provider: str,
    payload: TokenConnectRequest,
    ctx: RequestContext = Depends(can_manage),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Connect with a pasted API token (HubSpot, Shopify, Slack). Checked with the provider first, saved encrypted."""
    _provider_or_404(provider)
    try:
        conn = await manual_token.connect_with_token(
            db, tenant_id=ctx.tenant_id, user_id=ctx.user_id, provider=provider, token=payload.token, shop=payload.shop
        )
    except OAuthError as exc:
        await db.rollback()
        raise _http(exc) from exc
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="oauth.connected_with_token",
        resource_type="oauth_connection",
        resource_id=conn.id,
        extra={"provider": provider},
    )
    await db.commit()
    connection_health.forget(ctx.tenant_id)
    return {"provider": provider, "account_label": conn.account_label}


def _hub_redirect(return_url: str, **params: str) -> RedirectResponse:
    base = get_settings().frontend_base_url.rstrip("/")
    path = return_url or DEFAULT_RETURN_URL
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{base}{path}{sep}{urlencode(params, quote_via=quote)}", status_code=302)


@router.get("/{provider}/callback")
async def callback(
    provider: str, request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    """Public: the provider redirects the browser here. Never returns JSON;
    always lands the user back on the frontend with ?connected= or ?error=."""
    if provider not in PROVIDERS:
        return _hub_redirect(DEFAULT_RETURN_URL, error="unknown_provider", provider=provider[:32])
    params = {k: v for k, v in request.query_params.items()}
    try:
        result = await flow.handle_callback(db, provider_key=provider, params=params)
        await log_audit_event(
            db,
            tenant_id=result.connection.tenant_id,
            actor_user_id=result.connection.connected_by_user_id,
            action="oauth.connected",
            resource_type="oauth_connection",
            resource_id=result.connection.id,
            extra={"provider": provider},
        )
        await db.commit()
        connection_health.forget(result.connection.tenant_id)
    except OAuthError as exc:
        return _hub_redirect(
            getattr(exc, "return_url", DEFAULT_RETURN_URL),
            error=exc.code,
            error_description=exc.message[:200],
            provider=provider,
        )
    except Exception:
        logger.exception("oauth callback crashed for %s", provider)
        await db.rollback()
        return _hub_redirect(DEFAULT_RETURN_URL, error="server_error", provider=provider)
    return _hub_redirect(result.return_url, connected=provider)


@router.delete("/{provider}", status_code=204)
async def disconnect(
    provider: str,
    ctx: RequestContext = Depends(can_manage),
    db: AsyncSession = Depends(get_db),
) -> None:
    _provider_or_404(provider)
    conn = await get_connection(db, ctx.tenant_id, provider)
    if conn is None:
        raise HTTPException(404, detail={"code": "not_connected", "message": f"{provider} is not connected"})
    conn_id = conn.id
    await flow.disconnect(db, tenant_id=ctx.tenant_id, provider_key=provider)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="oauth.disconnected",
        resource_type="oauth_connection",
        resource_id=conn_id,
        extra={"provider": provider},
    )
    await db.commit()
    connection_health.forget(ctx.tenant_id)


@router.get("/{provider}/resources", response_model=ResourcesResponse)
async def provider_resources(
    provider: str, ctx: RequestContext = Depends(can_manage), db: AsyncSession = Depends(get_db)
) -> ResourcesResponse:
    _provider_or_404(provider)
    try:
        data = await resources.list_resources(db, ctx.tenant_id, provider)
    except OAuthError as exc:
        raise _http(exc) from exc
    return ResourcesResponse(**data)


@router.post("/{provider}/select")
async def provider_select(
    provider: str,
    payload: SelectRequest,
    ctx: RequestContext = Depends(can_manage),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _provider_or_404(provider)
    try:
        selected = await resources.select_resource(
            db,
            ctx.tenant_id,
            provider,
            resources.SelectPayload(
                resource_id=payload.resource_id,
                block_busy=payload.block_busy,
                events=payload.events,
            ),
        )
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action="oauth.resource_selected",
            resource_type="oauth_connection",
            extra={"provider": provider},
        )
        await db.commit()
    except OAuthError as exc:
        await db.rollback()
        raise _http(exc) from exc
    return {"selected": selected}

