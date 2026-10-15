"""OIDC authorization-code login with per-tenant configuration (sso_configs)."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.core.security import hash_password
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import MembershipStatus, Role, Tenant, TenantUser, User
from packages.database.models.platform import SsoConfig

STATE_TYPE = "sso_state"
HTTP_TIMEOUT = 10.0


class SsoError(Exception):
    """Raised with a user-presentable message."""


def redirect_uri(tenant_slug: str) -> str:
    return (
        f"{get_settings().public_base_url.rstrip('/')}/api/v1/platform/sso/{tenant_slug}/callback"
    )


async def load_config(db: AsyncSession, tenant_slug: str) -> tuple[Tenant, SsoConfig]:
    tenant = (
        await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    ).scalar_one_or_none()
    config = None
    if tenant is not None:
        config = (
            await db.execute(select(SsoConfig).where(SsoConfig.tenant_id == tenant.id))
        ).scalar_one_or_none()
    if tenant is None or config is None or not config.enabled:
        raise SsoError("Single sign-on is not configured for this workspace")
    return tenant, config


async def discover(issuer: str, client: httpx.AsyncClient) -> dict[str, Any]:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = await client.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        doc = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SsoError(
            f"Could not read the identity provider's OIDC discovery document: {exc}"
        ) from exc
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if key not in doc:
            raise SsoError(f"Identity provider discovery document is missing {key}")
    return doc


def make_state(tenant_slug: str, nonce: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    settings = get_settings()
    return jwt.encode(
        {
            "typ": STATE_TYPE,
            "tenant": tenant_slug,
            "nonce": nonce,
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def read_state(state: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(state, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise SsoError("Invalid or expired SSO state") from exc
    if payload.get("typ") != STATE_TYPE:
        raise SsoError("Invalid SSO state")
    return payload


def build_authorize_url(
    discovery: dict[str, Any], config: SsoConfig, tenant_slug: str, state: str, nonce: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": redirect_uri(tenant_slug),
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
        }
    )
    return f"{discovery['authorization_endpoint']}?{query}"


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


async def exchange_code(
    discovery: dict[str, Any],
    config: SsoConfig,
    tenant_slug: str,
    code: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    try:
        response = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(tenant_slug),
                "client_id": config.client_id,
                "client_secret": decrypt_secret(config.encrypted_client_secret),
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise SsoError(f"Token exchange failed: {exc}") from exc
    if response.status_code != 200:
        raise SsoError("The identity provider rejected the authorization code")
    body = response.json()
    if "id_token" not in body:
        raise SsoError("The identity provider returned no id_token")
    return body


async def verify_id_token(
    id_token: str,
    *,
    discovery: dict[str, Any],
    issuer: str,
    client_id: str,
    nonce: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    algorithms = ["RS256", "RS384", "RS512", "ES256"]
    try:
        kid = jwt.get_unverified_header(id_token).get("kid")
        jwks = (await client.get(discovery["jwks_uri"], timeout=HTTP_TIMEOUT)).json()
        candidates = [k for k in jwks.get("keys", []) if kid is None or k.get("kid") == kid]
    except (jwt.PyJWTError, httpx.HTTPError, ValueError) as exc:
        raise SsoError(f"id_token verification failed: {exc}") from exc

    claims: dict[str, Any] | None = None
    last: Exception | None = None
    for jwk in candidates:
        # Some IdPs put a trailing slash in `iss`; accept either spelling.
        for iss in (issuer.rstrip("/"), issuer.rstrip("/") + "/"):
            try:
                claims = jwt.decode(
                    id_token,
                    jwt.PyJWK(jwk).key,
                    algorithms=algorithms,
                    audience=client_id,
                    issuer=iss,
                )
                break
            except jwt.PyJWTError as exc:
                last = exc
        if claims is not None:
            break
    if claims is None:
        raise SsoError(f"id_token verification failed: {last or 'no matching signing key'}")
    if claims.get("nonce") != nonce:
        raise SsoError("id_token nonce mismatch")
    if not claims.get("email"):
        raise SsoError("The identity provider did not return an email address")
    if claims.get("email_verified") is False:
        raise SsoError("The identity provider reports this email as unverified")
    return claims


async def provision_user(
    db: AsyncSession, tenant: Tenant, config: SsoConfig, claims: dict[str, Any]
) -> TenantUser:
    """Find or create the user by email and make sure they are an active
    member of this tenant (new members get config.default_role_name)."""
    email = claims["email"].lower()
    domains = [d.lower().lstrip("@") for d in config.allowed_email_domains]
    if domains and email.rsplit("@", 1)[-1] not in domains:
        raise SsoError("Your email domain is not allowed to sign in to this workspace")

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            # Random unusable password: this account signs in through SSO
            # (it can still use password reset to set one).
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            full_name=claims.get("name") or email.split("@")[0],
            email_verified_at=datetime.now(UTC),
        )
        db.add(user)
        await db.flush()
    elif not user.is_active:
        raise SsoError("This account is disabled")

    membership = (
        await db.execute(
            select(TenantUser).where(
                TenantUser.tenant_id == tenant.id, TenantUser.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        role = (
            await db.execute(
                select(Role).where(Role.tenant_id.is_(None), Role.name == config.default_role_name)
            )
        ).scalar_one_or_none()
        if role is None:
            raise SsoError(f"Default SSO role {config.default_role_name!r} does not exist")
        membership = TenantUser(tenant_id=tenant.id, user_id=user.id, role_id=role.id)
        db.add(membership)
        await db.flush()
    elif membership.status != MembershipStatus.ACTIVE:
        raise SsoError("Your membership in this workspace is not active")
    return membership
