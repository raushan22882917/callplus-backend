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
from datetime import UTC, datetime

from app.core.api_keys import hash_api_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import McpApiKey


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
    token = _bearer_token(headers)
    if token is None:
        raise McpAuthError(
            "Missing or malformed Authorization header. Connect with "
            "'Authorization: Bearer <api key>', using a key created via "
            "POST /api/v1/mcp/api-keys."
        )

    result = await db.execute(select(McpApiKey).where(McpApiKey.hashed_key == hash_api_key(token)))
    key = result.scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        raise McpAuthError("Invalid or revoked API key")

    key.last_used_at = datetime.now(UTC)
    await db.flush()
    return key.tenant_id
