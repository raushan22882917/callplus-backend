from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.audit import AuditLogRead
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AuditLog

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("", response_model=list[AuditLogRead])
async def list_audit_logs(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLog]:
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.tenant_id == ctx.tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())
