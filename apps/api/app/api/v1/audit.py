import csv
import io
import uuid
from datetime import datetime

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.audit import AuditLogRead
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AuditLog, User

router = APIRouter(prefix="/audit-logs", tags=["audit"])

# One export is capped so a single request can't pull the whole table.
EXPORT_ROW_LIMIT = 10_000
# Cells starting with these are read as formulas by spreadsheet apps.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _filtered(
    query: Select,
    tenant_id: uuid.UUID,
    *,
    action: str | None,
    resource_type: str | None,
    actor_user_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
) -> Select:
    query = query.where(AuditLog.tenant_id == tenant_id)
    if action:
        # "api_key" matches api_key.created, api_key.rotated, ...; a full name matches itself.
        query = query.where(
            (AuditLog.action == action) | AuditLog.action.startswith(f"{action}.", autoescape=True)
        )
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    if actor_user_id:
        query = query.where(AuditLog.actor_user_id == actor_user_id)
    if since:
        query = query.where(AuditLog.created_at >= since)
    if until:
        query = query.where(AuditLog.created_at < until)
    return query


def _safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


@router.get("", response_model=list[AuditLogRead])
async def list_audit_logs(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=100),
    resource_type: str | None = Query(default=None, max_length=100),
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    ctx: RequestContext = Depends(require_permission(Permissions.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLog]:
    query = _filtered(
        select(AuditLog),
        ctx.tenant_id,
        action=action,
        resource_type=resource_type,
        actor_user_id=actor_user_id,
        since=since,
        until=until,
    )
    result = await db.execute(
        query.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.get("/export")
async def export_audit_logs(
    action: str | None = Query(default=None, max_length=100),
    resource_type: str | None = Query(default=None, max_length=100),
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    ctx: RequestContext = Depends(require_permission(Permissions.AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The filtered log as a CSV file, newest first, up to 10,000 rows."""
    if ctx.api_key_id is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Exports need a signed-in user")
    query = _filtered(
        select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.actor_user_id),
        ctx.tenant_id,
        action=action,
        resource_type=resource_type,
        actor_user_id=actor_user_id,
        since=since,
        until=until,
    )
    rows = (
        await db.execute(query.order_by(AuditLog.created_at.desc()).limit(EXPORT_ROW_LIMIT))
    ).all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time_utc", "actor", "action", "resource_type", "resource_id", "details"])
    for log, email in rows:
        writer.writerow(
            [
                log.created_at.isoformat(),
                _safe_cell(email or log.actor_user_id),
                _safe_cell(log.action),
                _safe_cell(log.resource_type),
                _safe_cell(log.resource_id),
                _safe_cell(log.extra and str(log.extra)),
            ]
        )
    # Recorded last, so the export never lists itself.
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="audit.exported",
        resource_type="audit_log",
        extra={"rows": len(rows), "action": action, "resource_type": resource_type},
    )
    await db.commit()
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )
