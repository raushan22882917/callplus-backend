import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AuditLog


async def log_audit_event(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    extra: dict | None = None,
) -> None:
    """Record a high-signal mutation (access control, deletions, billing) to
    audit_logs. Callers add this alongside their existing commit — it only
    flushes, matching TenantScopedRepository.create's pattern, so it never
    needs its own transaction."""

    db.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            extra=extra or {},
        )
    )
    await db.flush()
