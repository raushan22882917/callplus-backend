import logging
import secrets
import uuid
from datetime import timedelta

from app.api.v1.auth import PASSWORD_RESET_PURPOSE
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import (
    RequestContext,
    get_current_context,
    get_db,
    require_permission,
    require_platform_admin,
)
from app.core.exceptions import PermissionDeniedError
from app.core.permissions import Permissions
from app.core.security import create_purpose_token, hash_password
from app.integrations.email_client import send_email
from app.schemas.user import (
    GrantPlatformAdminRequest,
    InviteUserRequest,
    InviteUserResponse,
    MeResponse,
    PlatformAdminRead,
    TeamMemberRead,
    UpdateTeamMemberRequest,
)
from app.services import plans
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from packages.database.models import MembershipStatus, Role, TenantUser, User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["users"])


@router.get("/users/me", response_model=MeResponse)
async def get_me(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> MeResponse:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot read the owning user's profile")
    user = await db.get(User, ctx.user_id)
    return MeResponse(
        user_id=ctx.user_id,
        email=user.email,
        full_name=user.full_name,
        tenant_id=ctx.tenant_id,
        role_name=ctx.role_name,
        permission_codes=sorted(ctx.permission_codes),
        is_platform_admin=ctx.is_platform_admin,
    )


@router.get("/users", response_model=list[TeamMemberRead])
async def list_team_members(
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[TeamMemberRead]:
    result = await db.execute(
        select(TenantUser)
        .where(TenantUser.tenant_id == ctx.tenant_id)
        .options(joinedload(TenantUser.user), joinedload(TenantUser.role))
        .order_by(TenantUser.created_at.asc())
    )
    memberships = result.unique().scalars().all()
    return [
        TeamMemberRead(
            membership_id=m.id,
            user_id=m.user_id,
            email=m.user.email,
            full_name=m.user.full_name,
            role_name=m.role.name,
            status=m.status,
            created_at=m.created_at,
        )
        for m in memberships
    ]


@router.post(
    "/users/invite", response_model=InviteUserResponse, status_code=status.HTTP_201_CREATED
)
async def invite_user(
    payload: InviteUserRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> InviteUserResponse:
    await plans.ensure_can_create(db, ctx.tenant_id, "team_members")
    role = (
        await db.execute(
            select(Role).where(
                Role.name == payload.role_name,
                (Role.tenant_id == ctx.tenant_id) | (Role.tenant_id.is_(None)),
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role: {payload.role_name}")

    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()

    temporary_password: str | None = None
    if user is None:
        temporary_password = secrets.token_urlsafe(12)
        user = User(
            email=payload.email.lower(),
            full_name=payload.full_name,
            hashed_password=hash_password(temporary_password),
        )
        db.add(user)
        await db.flush()

    existing = (
        await db.execute(
            select(TenantUser).where(
                TenantUser.tenant_id == ctx.tenant_id, TenantUser.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "User is already a member of this tenant")

    # No email-based accept-invite flow exists yet: the temporary password is
    # handed back directly (see InviteUserResponse), so the membership is
    # usable immediately rather than stuck in INVITED with no way to activate.
    membership = TenantUser(
        tenant_id=ctx.tenant_id,
        user_id=user.id,
        role_id=role.id,
        status=MembershipStatus.ACTIVE,
    )
    db.add(membership)
    await db.flush()
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="user.invited",
        resource_type="tenant_user",
        resource_id=membership.id,
        extra={"email": user.email, "role_name": role.name},
    )
    await db.commit()

    settings = get_settings()
    emailed = False
    if settings.smtp_host:
        token = create_purpose_token(
            user_id=user.id, purpose=PASSWORD_RESET_PURPOSE, expires_delta=timedelta(days=7)
        )
        link = f"{settings.frontend_base_url}/reset-password?token={token}"
        try:
            await send_email(
                to_email=user.email,
                subject="You've been invited to the call center platform",
                html_body=f"<p>You've been added as {role.name}. "
                f"<a href='{link}'>Set your password</a> to sign in "
                "(link valid for 7 days).</p>",
            )
            emailed = True
        except Exception:
            logger.exception("failed to send invite email to %s", user.email)

    # Only echo the temporary password when the invitee could not be emailed.
    return InviteUserResponse(
        membership_id=membership.id,
        email=user.email,
        temporary_password=None if emailed else temporary_password,
        email_sent=emailed,
    )


async def _count_other_active_owners(
    db: AsyncSession, *, tenant_id: uuid.UUID, exclude_membership_id: uuid.UUID
) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(TenantUser)
        .join(Role, TenantUser.role_id == Role.id)
        .where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.id != exclude_membership_id,
            TenantUser.status == MembershipStatus.ACTIVE,
            Role.name == "OWNER",
        )
    )
    return result.scalar_one()


@router.patch("/users/{membership_id}", response_model=TeamMemberRead)
async def update_team_member(
    membership_id: uuid.UUID,
    payload: UpdateTeamMemberRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.USERS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> TeamMemberRead:
    result = await db.execute(
        select(TenantUser)
        .where(TenantUser.id == membership_id, TenantUser.tenant_id == ctx.tenant_id)
        .options(joinedload(TenantUser.user), joinedload(TenantUser.role))
    )
    membership = result.unique().scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team member not found")

    is_currently_active_owner = (
        membership.role.name == "OWNER" and membership.status == MembershipStatus.ACTIVE
    )
    would_lose_owner = is_currently_active_owner and (
        (payload.role_name is not None and payload.role_name != "OWNER")
        or (payload.status is not None and payload.status != MembershipStatus.ACTIVE)
    )
    if would_lose_owner:
        remaining = await _count_other_active_owners(
            db, tenant_id=ctx.tenant_id, exclude_membership_id=membership.id
        )
        if remaining == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Cannot remove the tenant's only active owner"
            )

    if payload.role_name is not None:
        role = (
            await db.execute(
                select(Role).where(
                    Role.name == payload.role_name,
                    (Role.tenant_id == ctx.tenant_id) | (Role.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role: {payload.role_name}")
        membership.role_id = role.id
        membership.role = role

    if payload.status is not None:
        membership.status = payload.status

    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="user.membership_updated",
        resource_type="tenant_user",
        resource_id=membership.id,
        extra={
            "role_name": payload.role_name,
            "status": payload.status.value if payload.status else None,
        },
    )
    await db.commit()
    await db.refresh(membership)

    return TeamMemberRead(
        membership_id=membership.id,
        user_id=membership.user_id,
        email=membership.user.email,
        full_name=membership.user.full_name,
        role_name=membership.role.name,
        status=membership.status,
        created_at=membership.created_at,
    )


# --- Platform-admin management ---
#
# is_platform_admin is a global, cross-tenant flag (RequestContext.has_permission
# short-circuits true for it, bypassing every tenant permission check — see
# app/core/deps.py) so, like the Plan catalog in app/api/v1/billing.py, managing
# it is gated on being a platform admin already, not on any tenant permission.
# The very first platform admin still has to be set directly in the database —
# there is no bootstrap route, intentionally, since any HTTP-reachable bootstrap
# would itself be a privilege-escalation hole.


@router.get("/users/admin/platform-admins", response_model=list[PlatformAdminRead])
async def list_platform_admins(
    ctx: RequestContext = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    result = await db.execute(
        select(User).where(User.is_platform_admin.is_(True)).order_by(User.email)
    )
    return list(result.scalars().all())


@router.post("/users/admin/platform-admins", response_model=PlatformAdminRead)
async def grant_platform_admin(
    payload: GrantPlatformAdminRequest,
    ctx: RequestContext = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No account with this email — they must sign up first",
        )

    user.is_platform_admin = True
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="platform_admin.granted",
        resource_type="user",
        resource_id=user.id,
        extra={"email": user.email},
    )
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/admin/platform-admins/{user_id}", response_model=PlatformAdminRead)
async def revoke_platform_admin(
    user_id: uuid.UUID,
    ctx: RequestContext = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await db.get(User, user_id)
    if user is None or not user.is_platform_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such platform admin")

    if user.id == ctx.user_id:
        remaining = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.is_platform_admin.is_(True), User.id != user.id)
            )
        ).scalar_one()
        if remaining == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Cannot remove the last remaining platform admin"
            )

    user.is_platform_admin = False
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="platform_admin.revoked",
        resource_type="user",
        resource_id=user.id,
        extra={"email": user.email},
    )
    await db.commit()
    await db.refresh(user)
    return user
