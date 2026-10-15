import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.api_keys import API_KEY_PREFIXES, hash_api_key, permissions_for_scopes
from app.core.exceptions import PermissionDeniedError
from app.core.ip_allowlist import enforce_ip_allowlist
from app.core.rate_limit import enforce_rate_limit
from app.core.security import InvalidTokenError, TokenPayload, TokenType, decode_token
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from packages.database.models import (
    ApiKey,
    ApiKeyUsageDaily,
    MembershipStatus,
    Role,
    Tenant,
    TenantStatus,
    TenantUser,
)
from packages.database.session import get_session, session_scope, set_tenant_context

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


async def get_access_token_payload(
    token: str | None = Depends(oauth2_scheme),
) -> TokenPayload:
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        return decode_token(token, expected_type=TokenType.ACCESS)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc


@dataclass(frozen=True)
class RequestContext:
    """The single, server-verified source of truth for "who is making this
    request and which tenant do they act on behalf of". Every tenant-scoped
    query MUST use ctx.tenant_id — never a tenant_id read from the request
    body/query/path.
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    membership_id: uuid.UUID
    role_name: str
    permission_codes: frozenset[str]
    is_platform_admin: bool
    # Set only when the request authenticated with a public API key (ak_...)
    # instead of a user session.
    api_key_id: uuid.UUID | None = None
    api_key_mode: str | None = None
    api_key_scopes: frozenset[str] = frozenset()

    def has_permission(self, code: str) -> bool:
        return self.is_platform_admin or code in self.permission_codes


async def _record_api_key_use(tenant_id: uuid.UUID, key_id: uuid.UUID) -> None:
    """last_used_at + the per-day request counter, in their own short session
    so they are saved even on read-only requests that never commit. Never
    allowed to fail the request itself."""
    try:
        async with session_scope() as db:
            await set_tenant_context(db, tenant_id)
            now = datetime.now(UTC)
            row = await db.get(ApiKey, key_id)
            if row is not None:
                row.last_used_at = now
            stmt = pg_insert(ApiKeyUsageDaily).values(
                tenant_id=tenant_id, api_key_id=key_id, day=now.date(), requests=1
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_api_key_usage_key_day",
                    set_={"requests": ApiKeyUsageDaily.requests + 1},
                )
            )
            await db.commit()
    except Exception:
        logger.warning("failed to record api key usage", exc_info=True)


async def context_from_api_key(
    token: str, db: AsyncSession, request: Request | None = None
) -> RequestContext:
    key = (
        await db.execute(select(ApiKey).where(ApiKey.hashed_key == hash_api_key(token)))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        key is None
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now)
        or key.created_by_user_id is None
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid, expired or revoked API key")

    tenant = await db.get(Tenant, key.tenant_id)
    if tenant is None or tenant.status not in (TenantStatus.ACTIVE, TenantStatus.TRIAL):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")

    await enforce_rate_limit(key.id)
    await set_tenant_context(db, key.tenant_id)
    if request is not None:
        await enforce_ip_allowlist(request, db, key.tenant_id)
    await _record_api_key_use(key.tenant_id, key.id)

    return RequestContext(
        user_id=key.created_by_user_id,
        tenant_id=key.tenant_id,
        # No membership behind an API key; the key id stands in so audit trails
        # and logs can still tell requests apart.
        membership_id=key.id,
        role_name=f"API_KEY:{key.mode}",
        permission_codes=permissions_for_scopes(key.scopes),
        is_platform_admin=False,
        api_key_id=key.id,
        api_key_mode=key.mode,
        api_key_scopes=frozenset(key.scopes),
    )


async def get_current_context(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> RequestContext:
    if token is not None and token.startswith(API_KEY_PREFIXES):
        return await context_from_api_key(token, db, request)
    payload = await get_access_token_payload(token)
    result = await db.execute(
        select(TenantUser)
        .where(TenantUser.id == uuid.UUID(payload.membership_id))
        .options(
            joinedload(TenantUser.user),
            joinedload(TenantUser.tenant),
            joinedload(TenantUser.role).joinedload(Role.permissions),
        )
    )
    membership = result.unique().scalar_one_or_none()

    if (
        membership is None
        or str(membership.tenant_id) != payload.tenant_id
        or str(membership.user_id) != payload.sub
        or membership.status != MembershipStatus.ACTIVE
        or not membership.user.is_active
        or membership.tenant.status not in (TenantStatus.ACTIVE, TenantStatus.TRIAL)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session is no longer valid")

    # Platform admins legitimately work across tenants, so they are not
    # confined by row-level security; everyone else is scoped to their tenant.
    if not membership.user.is_platform_admin:
        await set_tenant_context(db, membership.tenant_id)
    await enforce_ip_allowlist(
        request, db, membership.tenant_id, bypass=membership.user.is_platform_admin
    )

    return RequestContext(
        user_id=membership.user_id,
        tenant_id=membership.tenant_id,
        membership_id=membership.id,
        role_name=membership.role.name,
        permission_codes=frozenset(p.code for p in membership.role.permissions),
        is_platform_admin=membership.user.is_platform_admin,
    )


def require_permission(*codes: str):
    """Dependency factory: require the caller's role to grant ALL of `codes`."""

    async def checker(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
        missing = [c for c in codes if not ctx.has_permission(c)]
        if missing:
            raise PermissionDeniedError(f"Missing permission(s): {', '.join(missing)}")
        return ctx

    return checker


async def require_platform_admin(
    ctx: RequestContext = Depends(get_current_context),
) -> RequestContext:
    """Gate for platform-wide resources (e.g. the shared billing Plan
    catalog) that must NOT be editable by a tenant's own `billing.manage`
    permission — that permission only grants control over that one tenant's
    subscription, not the catalog every tenant reads from. Deliberately
    ignores permission_codes and checks is_platform_admin directly, unlike
    require_permission()/has_permission() which treat it as a bypass-all."""
    if not ctx.is_platform_admin:
        raise PermissionDeniedError("This action requires a platform admin account")
    return ctx
