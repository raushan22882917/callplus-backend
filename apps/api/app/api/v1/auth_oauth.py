"""'Sign in with Google / Microsoft'. Finds the user by the provider-verified
email and issues the normal JWT pair. A plain sign-in never creates a user or a
tenant: an unknown email is sent back to /login with a clear error. Only the
sign-up form's button (mode=signup) may create a workspace, and only for an
email the provider has verified; if that email already has an account it is
simply signed in.

Redirect URI to register at the provider (separate from the connect flow):
  {PUBLIC_BASE_URL}/api/v1/auth/oauth/{provider}/callback
The browser ends on {FRONTEND_BASE_URL}/sso/callback#access_token=...,
the same landing page the tenant-OIDC SSO uses."""

import logging
from urllib.parse import quote, urlencode

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import get_db
from app.core.rate_limit_ip import ip_rate_limit
from app.core.security import create_access_token, create_refresh_token
from app.integrations.oauth import login as login_mod
from app.integrations.oauth import state as state_mod
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import get_provider
from app.services import workspace_signup
from app.services.platform_totp import get_enabled_totp
from app.services.user_sessions import alert_new_sign_in, create_session
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import User

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


@router.get("/{provider}/login", dependencies=[Depends(ip_rate_limit("oauth-login", limit=30))])
async def oauth_login(
    provider: str,
    tenant_slug: str | None = Query(default=None, max_length=100),
    mode: str = Query(default="login", pattern="^(login|signup)$"),
    company_name: str | None = Query(default=None, max_length=120),
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
            extra={
                **({"tenant_slug": tenant_slug} if tenant_slug else {}),
                **({"mode": "signup", **({"company_name": company_name.strip()} if (company_name or "").strip() else {})} if mode == "signup" else {}),
            }
            or None,
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
        created = False
        existing = (await db.execute(select(User.id).where(User.email == idc["email"]))).first()
        if claims.extra.get("mode") == "signup" and existing is None:
            _, user, membership = await workspace_signup.create_from_identity(
                db, email=idc["email"], name=idc.get("name"), company_name=claims.extra.get("company_name")
            )
            created = True
        else:
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
            action="oauth.signup" if created else "oauth.login",
            resource_type="user",
            resource_id=user.id,
            extra={"provider": provider},
        )
        session = await create_session(
            db, user_id=user.id, tenant_id=membership.tenant_id, request=request
        )
        await db.commit()
        await alert_new_sign_in(db, user, session)
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
                user_id=user.id,
                membership_id=membership.id,
                tenant_id=membership.tenant_id,
                session_id=session.id,
            ),
            "refresh_token": create_refresh_token(
                user_id=user.id,
                membership_id=membership.id,
                tenant_id=membership.tenant_id,
                session_id=session.id,
            ),
        },
        quote_via=quote,
    )
    return RedirectResponse(
        f"{get_settings().frontend_base_url.rstrip('/')}/sso/callback#{fragment}", status_code=302
    )
