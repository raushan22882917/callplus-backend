"""Per-tenant custom roles. System roles (OWNER, ADMIN, ...) have a NULL
tenant_id and are shared and read-only; a tenant's own roles carry its
tenant_id. Permissions are re-read from the role on every request (see
app/core/deps.py), so editing a role takes effect on its members' next call."""

import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.role import PermissionRead, RoleCreate, RoleRead, RoleUpdate
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from packages.database.models import Permission, Role, TenantUser

router = APIRouter(tags=["roles"])


def _to_read(role: Role) -> RoleRead:
    return RoleRead(
        id=role.id,
        name=role.name,
        is_system=role.is_system,
        permission_codes=sorted(p.code for p in role.permissions),
    )


async def _resolve_permissions(
    db: AsyncSession, ctx: RequestContext, codes: list[str]
) -> list[Permission]:
    unique = sorted(set(codes))
    permissions = (
        (await db.execute(select(Permission).where(Permission.code.in_(unique)))).scalars().all()
    )
    unknown = set(unique) - {p.code for p in permissions}
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown permission(s): {sorted(unknown)}"
        )
    # No privilege escalation: you can only hand out what you hold yourself.
    not_held = [c for c in unique if not ctx.has_permission(c)]
    if not_held:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"You cannot grant permission(s) you lack: {not_held}"
        )
    return list(permissions)


async def _name_is_taken(
    db: AsyncSession, ctx: RequestContext, name: str, *, exclude_id: uuid.UUID | None = None
) -> bool:
    # System role names are reserved for every tenant too: role lookups by
    # name search "this tenant's roles + system roles" and must stay unique.
    query = (
        select(func.count())
        .select_from(Role)
        .where(
            func.lower(Role.name) == name.lower(),
            or_(Role.tenant_id == ctx.tenant_id, Role.tenant_id.is_(None)),
        )
    )
    if exclude_id is not None:
        query = query.where(Role.id != exclude_id)
    return (await db.execute(query)).scalar_one() > 0


async def _get_custom_role(db: AsyncSession, ctx: RequestContext, role_id: uuid.UUID) -> Role:
    role = (
        await db.execute(
            select(Role)
            .where(
                Role.id == role_id,
                or_(Role.tenant_id == ctx.tenant_id, Role.tenant_id.is_(None)),
            )
            .options(selectinload(Role.permissions))
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    if role.is_system or role.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "System roles cannot be modified")
    return role


@router.get("/permissions", response_model=list[PermissionRead])
async def list_permissions(
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Permission]:
    return list((await db.execute(select(Permission).order_by(Permission.code))).scalars().all())


@router.get("/roles", response_model=list[RoleRead])
async def list_roles(
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[RoleRead]:
    roles = (
        (
            await db.execute(
                select(Role)
                .where(or_(Role.tenant_id == ctx.tenant_id, Role.tenant_id.is_(None)))
                .options(selectinload(Role.permissions))
                .order_by(Role.is_system.desc(), Role.name)
            )
        )
        .scalars()
        .all()
    )
    return [_to_read(r) for r in roles]


@router.post("/roles", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> RoleRead:
    name = payload.name.strip()
    if await _name_is_taken(db, ctx, name):
        raise HTTPException(status.HTTP_409_CONFLICT, "A role with this name already exists")
    permissions = await _resolve_permissions(db, ctx, payload.permission_codes)

    role = Role(tenant_id=ctx.tenant_id, name=name, is_system=False, permissions=permissions)
    db.add(role)
    await db.flush()
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="role.created",
        resource_type="role",
        resource_id=role.id,
        extra={"name": name, "permissions": sorted(p.code for p in permissions)},
    )
    await db.commit()
    return _to_read(role)


@router.patch("/roles/{role_id}", response_model=RoleRead)
async def update_role(
    role_id: uuid.UUID,
    payload: RoleUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> RoleRead:
    role = await _get_custom_role(db, ctx, role_id)
    if payload.name is not None:
        name = payload.name.strip()
        if await _name_is_taken(db, ctx, name, exclude_id=role.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "A role with this name already exists")
        role.name = name
    if payload.permission_codes is not None:
        role.permissions = await _resolve_permissions(db, ctx, payload.permission_codes)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="role.updated",
        resource_type="role",
        resource_id=role.id,
        extra={"name": role.name, "permissions": sorted(p.code for p in role.permissions)},
    )
    await db.commit()
    return _to_read(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    role = await _get_custom_role(db, ctx, role_id)
    in_use = (
        await db.execute(
            select(func.count()).select_from(TenantUser).where(TenantUser.role_id == role.id)
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Role is assigned to {in_use} member(s); reassign them first"
        )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="role.deleted",
        resource_type="role",
        resource_id=role.id,
        extra={"name": role.name},
    )
    await db.delete(role)
    await db.commit()
