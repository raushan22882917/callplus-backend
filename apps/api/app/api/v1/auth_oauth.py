"""'Sign in with Google / Microsoft'. Identity only: finds the user by the
provider-verified email and issues the normal JWT pair. It never creates a
user or a tenant; an unknown email is sent back to /login with a clear error.

Redirect URI to register at the provider (separate from the connect flow):
  {PUBLIC_BASE_URL}/api/v1/auth/oauth/{provider}/callback
The browser ends on {FRONTEND_BASE_URL}/sso/callback#access_token=...,
the same landing page the tenant-OIDC SSO uses."""

import logging
from urllib.parse import quote, urlencode

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import get_db
from app.core.security import create_access_token, create_refresh_token
from app.integrations.oauth import login as login_mod
from app.integrations.oauth import state as state_mod
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import get_provider
from app.services.platform_totp import get_enabled_totp
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth/oauth", tags=["auth"])
logger = logging.getLogger(__name__)


def _login_error(code: str, message: str) -> RedirectResponse:
    base = get_settings().frontend_base_url.rstrip("/")
    query = urlencode({"sso_error": message, "oauth_error": code}, quote_via=quote)
    return RedirectResponse(f"{base}/login?{query}", status_code=302)


def _login_provider(key: str):
    if key not in login_mod.LOGIN_PROVIDERS:
        raise OAuthError("unknown_provider", f"Sign in with {key} is not supported", status_code=404)
    return get_provider(key)


@router.get("/providers")
async def login_providers() -> dict[str, list[str]]:
    """Which 'Sign in with ...' buttons the login page should show."""
    return {
        "providers": [k for k in login_mod.LOGIN_PROVIDERS if get_provider(k).configured()]
    }


@router.get("/{provider}/login")
async def oauth_login(
    provider: str,
    tenant_slug: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    try:
        prov = _login_provider(provider)
        prov.require_configured()
        token, challenge, nonce = await state_mod.create_state(
            db,
            provider=provider,
            purpose="login",
            tenant_id=None,
            user_id=None,
            return_url="/",
            extra={"tenant_slug": tenant_slug} if tenant_slug else None,
            use_pkce=prov.pkce,
        )
        redirect = login_mod.login_redirect(provider)
        url = prov.build_authorize_url(
            state=token, scopes=login_mod.LOGIN_SCOPES,
            code_challenge=challenge,
            redirect=redirect,
            for_login=True,
        )
        # OIDC nonce = our state nonce: binds the id_token to this request.
        url += "&" + urlencode({"nonce": nonce})
    except OAuthError as exc:
        return _login_error(exc.code, exc.message)
    return RedirectResponse(url, status_code=302)


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str, request: Request, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    params = dict(request.query_params)
    try:
        prov = _login_provider(provider)
        state = params.get("state")
        if not state:
            raise OAuthError("invalid_state", "Missing authorization state")
        claims = await state_mod.consume_state(db, state, provider=provider, purpose="login")
        if params.get("error"):
            raise OAuthError("access_denied", params.get("error_description") or params["error"])
        code = params.get("code")
        if not code:
            raise OAuthError("invalid_callback", "The provider did not return an authorization code")
        async with new_client() as client:
            token = await prov.exchange_code(
                client,
                code=code,
                code_verifier=claims.code_verifier,
                redirect=login_mod.login_redirect(provider),
            )
        id_token = token.raw.get("id_token")
        if not id_token:
            raise OAuthError("invalid_id_token", "The identity provider returned no id_token")
        idc = login_mod.claims_from_id_token(provider, id_token, nonce=claims.nonce)
        user, membership = await login_mod.find_login_membership(
            db, idc["email"], claims.extra.get("tenant_slug")
        )
        if await get_enabled_totp(db, user.id) is not None:
            raise OAuthError(
                "two_factor_required",
                "This account uses two-factor authentication; sign in with your password and code.",
                status_code=403,
            )
        if user.email_verified_at is None:
            from datetime import UTC, datetime

            user.email_verified_at = datetime.now(UTC)  # the IdP verified it
        await log_audit_event(
            db,
            tenant_id=membership.tenant_id,
            actor_user_id=user.id,
            action="oauth.login",
            resource_type="user",
            resource_id=user.id,
            extra={"provider": provider},
        )
        await db.commit()
    except OAuthError as exc:
        await db.rollback()
        return _login_error(exc.code, exc.message)
    except Exception:
        logger.exception("oauth login callback crashed for %s", provider)
        await db.rollback()
        return _login_error("server_error", "Sign-in failed. Please try again.")
    fragment = urlencode(
        {
            "access_token": create_access_token(
                user_id=user.id, membership_id=membership.id, tenant_id=membership.tenant_id
            ),
            "refresh_token": create_refresh_token(
                user_id=user.id, membership_id=membership.id, tenant_id=membership.tenant_id
            ),
        },
        quote_via=quote,
    )
    return RedirectResponse(
        f"{get_settings().frontend_base_url.rstrip('/')}/sso/callback#{fragment}", status_code=302
    )
