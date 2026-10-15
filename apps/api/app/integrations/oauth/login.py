"""'Sign in with Google / Microsoft': identity only, never provisioning.

The id_token is read straight from the token endpoint over TLS (server to
server), where OIDC Core 3.1.3.7 lets us skip signature verification; we still
validate issuer, audience, expiry and the nonce that is bound to our signed
state. The email must be verified by the identity provider, and must already
belong to an active user: we never create users or tenants here."""

import re
import time
from typing import Any

import jwt
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.providers import MS_CONSUMER_TENANT, get_provider, login_redirect_uri
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    MembershipStatus,
    Tenant,
    TenantStatus,
    TenantUser,
    User,
)

LOGIN_PROVIDERS = ("google", "microsoft")
LOGIN_SCOPES = ["openid", "email", "profile"]
_GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
_MS_ISSUER_RE = re.compile(r"^https://login\.microsoftonline\.com/([0-9a-f\-]{36})/v2\.0$")


def login_redirect(provider: str) -> str:
    return login_redirect_uri(provider)


def claims_from_id_token(
    provider_key: str, id_token: str, *, nonce: str, now: float | None = None
) -> dict[str, Any]:
    provider = get_provider(provider_key)
    try:
        claims = jwt.decode(
            id_token, options={"verify_signature": False, "verify_aud": False, "verify_exp": False}
        )
    except jwt.PyJWTError as exc:
        raise OAuthError("invalid_id_token", "The identity provider returned an unreadable token") from exc
    now = now if now is not None else time.time()
    if claims.get("exp") is None or claims["exp"] < now - 60:
        raise OAuthError("invalid_id_token", "The identity token has expired")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if provider.client_id() not in audiences:
        raise OAuthError("invalid_id_token", "The identity token was issued for a different app")
    if claims.get("nonce") != nonce:
        raise OAuthError("invalid_id_token", "The identity token nonce does not match")

    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in email:
        raise OAuthError("email_missing", "The identity provider did not share an email address")

    if provider_key == "google":
        if claims.get("iss") not in _GOOGLE_ISSUERS:
            raise OAuthError("invalid_id_token", "Unexpected token issuer")
        if claims.get("email_verified") is not True:
            raise OAuthError("email_not_verified", "Your Google email address is not verified")
    else:
        match = _MS_ISSUER_RE.match(claims.get("iss") or "")
        if not match or match.group(1) != claims.get("tid"):
            raise OAuthError("invalid_id_token", "Unexpected token issuer")
        # Microsoft does not guarantee that `email` is verified for work/school
        # tenants (nOAuth). Trust it only for personal accounts, or when the
        # app registration enables the `xms_edov` optional claim.
        verified = claims.get("tid") == MS_CONSUMER_TENANT or claims.get("xms_edov") in (True, 1, "1", "true")
        if not verified:
            raise OAuthError(
                "email_not_verified",
                "Microsoft did not confirm ownership of this email address "
                "(enable the xms_edov optional claim, see docs/OAUTH_SETUP.md)",
            )
    claims["email"] = email
    return claims


async def find_login_membership(
    db: AsyncSession, email: str, tenant_slug: str | None
) -> tuple[User, TenantUser]:
    """The active membership to sign in to, for an *existing* user only."""
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise OAuthError(
            "no_account",
            "No account exists for this email address. Ask your workspace admin to invite you.",
            status_code=403,
        )
    query = (
        select(TenantUser)
        .join(Tenant, TenantUser.tenant_id == Tenant.id)
        .where(
            TenantUser.user_id == user.id,
            TenantUser.status == MembershipStatus.ACTIVE,
            Tenant.status.in_([TenantStatus.ACTIVE, TenantStatus.TRIAL]),
        )
    )
    if tenant_slug:
        query = query.where(Tenant.slug == tenant_slug)
    memberships = list((await db.execute(query)).scalars().all())
    if not memberships:
        raise OAuthError(
            "no_membership", "Your account has no active workspace membership.", status_code=403
        )
    if len(memberships) > 1:
        raise OAuthError(
            "tenant_selection_required",
            "Your email belongs to several workspaces. Enter your workspace slug and try again.",
            status_code=409,
        )
    return user, memberships[0]
