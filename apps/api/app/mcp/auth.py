"""Resolves which tenant an MCP request acts on behalf of.

MCP clients (ChatGPT, Claude Desktop/web, or any other MCP host) have no
human login session, so unlike app/core/deps.py's RequestContext, there is
no JWT here — the client authenticates with a per-tenant API key (see
POST /api/v1/mcp/api-keys, packages/database/models/mcp.py) sent as a plain
`Authorization: Bearer <key>` header, which app/mcp/server.py reads straight
off the transport (mcp.server.mcpserver.Context.headers) rather than
plugging into the SDK's OAuth-shaped auth machinery — there is no
authorization server here, just one opaque token per tenant.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.api_keys import API_KEY_PREFIXES, hash_api_key
from app.core.mcp_oauth import ACCESS_PREFIX
from app.core.rate_limit import enforce_rate_limit
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    ApiKey,
    McpApiKey,
    McpOAuthToken,
    MembershipStatus,
    Tenant,
    TenantStatus,
    TenantUser,
    User,
)


@dataclass(frozen=True)
class McpPrincipal:
    """Who an MCP request acts as. `scopes` is None for a dedicated MCP key
    (full tool access, the original behaviour) and the key's scope list for a
    developer API key (ak_live_/ak_test_), which a tool call is checked
    against — so one key format works for both the REST API and MCP."""

    tenant_id: uuid.UUID
    key_id: uuid.UUID
    user_id: uuid.UUID | None
    scopes: frozenset[str] | None

    def allows(self, scope: str) -> bool:
        if self.scopes is None:
            return True
        family = scope.split(":")[0]
        # A write scope implies read of the same family.
        return scope in self.scopes or (scope.endswith(":read") and f"{family}:write" in self.scopes)


class McpAuthError(Exception):
    """The Authorization header was missing, malformed, or named a key that
    doesn't grant access — safe to surface verbatim to the MCP client."""


def _bearer_token(headers: Mapping[str, str] | None) -> str | None:
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() != "authorization":
            continue
        scheme, _, token = value.partition(" ")
        return token if scheme.lower() == "bearer" and token else None
    return None


async def resolve_tenant(headers: Mapping[str, str] | None, db: AsyncSession) -> uuid.UUID:
    return (await resolve_principal(headers, db)).tenant_id


async def valid_oauth_token(db: AsyncSession, token: str) -> McpOAuthToken | None:
    """The live OAuth access-token row for `token`, or None if unknown, expired,
    revoked, or its user/membership/tenant is no longer active."""
    row = (
        await db.execute(
            select(McpOAuthToken).where(McpOAuthToken.hashed_access_token == hash_api_key(token))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.access_expires_at <= datetime.now(UTC):
        return None
    user = await db.get(User, row.user_id)
    membership = (
        await db.execute(
            select(TenantUser).where(
                TenantUser.user_id == row.user_id,
                TenantUser.tenant_id == row.tenant_id,
                TenantUser.status == MembershipStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or membership is None:
        return None
    return row


async def resolve_principal(headers: Mapping[str, str] | None, db: AsyncSession) -> McpPrincipal:
    token = _bearer_token(headers)
    if token is None:
        raise McpAuthError(
            "Missing or malformed Authorization header. Connect with "
            "'Authorization: Bearer <api key>', using a key created via "
            "POST /api/v1/mcp/api-keys."
        )

    hashed = hash_api_key(token)
    now = datetime.now(UTC)
    if token.startswith(ACCESS_PREFIX):
        oauth_row = await valid_oauth_token(db, token)
        if oauth_row is None:
            raise McpAuthError("Invalid or expired access token")
        key_id, tenant_id = oauth_row.id, oauth_row.tenant_id
        user_id, scopes = oauth_row.user_id, frozenset(oauth_row.scopes)
        oauth_row.last_used_at = now
    elif token.startswith(API_KEY_PREFIXES):
        dev_key = (
            await db.execute(select(ApiKey).where(ApiKey.hashed_key == hashed))
        ).scalar_one_or_none()
        if (
            dev_key is None
            or dev_key.revoked_at is not None
            or (dev_key.expires_at is not None and dev_key.expires_at <= now)
        ):
            raise McpAuthError("Invalid, expired or revoked API key")
        key_id, tenant_id = dev_key.id, dev_key.tenant_id
        user_id, scopes = dev_key.created_by_user_id, frozenset(dev_key.scopes or [])
        dev_key.last_used_at = now
    else:
        key = (
            await db.execute(select(McpApiKey).where(McpApiKey.hashed_key == hashed))
        ).scalar_one_or_none()
        if key is None or key.revoked_at is not None:
            raise McpAuthError("Invalid or revoked API key")
        key_id, tenant_id = key.id, key.tenant_id
        user_id, scopes = key.created_by_user_id, None
        key.last_used_at = now

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or tenant.status not in (TenantStatus.ACTIVE, TenantStatus.TRIAL):
        raise McpAuthError("Account is not active")
    try:
        await enforce_rate_limit(key_id)
    except HTTPException as exc:
        raise McpAuthError(str(exc.detail)) from exc
    await db.flush()
    return McpPrincipal(tenant_id=tenant_id, key_id=key_id, user_id=user_id, scopes=scopes)
