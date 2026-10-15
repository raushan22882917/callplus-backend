import uuid
from datetime import UTC, datetime

from app.core.api_keys import generate_api_key
from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.mcp import McpApiKeyCreate, McpApiKeyCreated, McpApiKeyRead
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import McpApiKey

router = APIRouter(prefix="/mcp/api-keys", tags=["mcp"])


class McpApiKeyRepository(TenantScopedRepository[McpApiKey]):
    model = McpApiKey


@router.get("", response_model=list[McpApiKeyRead])
async def list_mcp_api_keys(
    ctx: RequestContext = Depends(require_permission(Permissions.MCP_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[McpApiKey]:
    return await McpApiKeyRepository(db, ctx.tenant_id).list()


@router.post("", response_model=McpApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_mcp_api_key(
    payload: McpApiKeyCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.MCP_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> McpApiKeyCreated:
    """Mint a new key for connecting an external MCP client (ChatGPT, Claude,
    or any other MCP host — see app/mcp/server.py) to this tenant. The
    plaintext key is only ever present in this one response."""
    plaintext_key, prefix, hashed = generate_api_key()
    key = await McpApiKeyRepository(db, ctx.tenant_id).create(
        name=payload.name,
        key_prefix=prefix,
        hashed_key=hashed,
        created_by_user_id=ctx.user_id,
    )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="mcp_api_key.created",
        resource_type="mcp_api_key",
        resource_id=key.id,
    )
    await db.commit()
    await db.refresh(key)
    return McpApiKeyCreated(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        api_key=plaintext_key,
        created_at=key.created_at,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_api_key(
    key_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.MCP_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    repo = McpApiKeyRepository(db, ctx.tenant_id)
    key = await repo.get(key_id)  # 404s if not in this tenant
    key.revoked_at = datetime.now(UTC)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="mcp_api_key.revoked",
        resource_type="mcp_api_key",
        resource_id=key.id,
    )
    await db.commit()
