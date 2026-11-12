import logging
import uuid
from datetime import timedelta

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_access_token_payload, get_current_context, get_db
from app.core.exceptions import ConflictError, InvalidCredentialsError, PermissionDeniedError
from app.core.rate_limit_ip import ip_rate_limit
from app.core.security import (
    InvalidTokenError,
    TokenType,
    burn_password_check,
    create_access_token,
    create_purpose_token,
    create_refresh_token,
    decode_purpose_token_full,
    decode_token,
    hash_password,
    password_fingerprint,
    verify_password,
)
from app.integrations.email_client import send_email
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    RefreshRequest,
    ResetPasswordRequest,
    RevokedCount,
    SessionRead,
    SignupRequest,
    SwitchTenantRequest,
    TenantOption,
    TokenPair,
)
from app.services import workspace_signup
from app.services.platform_totp import get_enabled_totp, verify_second_factor
from app.services.user_sessions import (
    alert_new_sign_in,
    create_session,
    describe_device,
    list_active,
    revoke,
    revoke_others,
    session_is_active,
    touch,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from packages.database.models import (
    MembershipStatus,
    Tenant,
    TenantStatus,
    TenantUser,
    User,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()
_limit_login = ip_rate_limit("login", limit=_settings.auth_login_rate_limit_per_minute)
_limit_signup = ip_rate_limit("signup", limit=_settings.auth_signup_rate_limit_per_minute)
_limit_reset = ip_rate_limit("reset", limit=_settings.auth_reset_rate_limit_per_minute)
logger = logging.getLogger(__name__)

PASSWORD_RESET_PURPOSE = "password_reset"
# Generic response for forgot-password regardless of whether the email
# matched an account — avoids leaking which emails have accounts.
_FORGOT_PASSWORD_MESSAGE = "If an account with that email exists, a reset link has been sent."


async def _issue_tokens(
    db: AsyncSession,
    membership: TenantUser,
    request: Request,
    *,
    session_id: uuid.UUID | None = None,
    announce: bool = False,
) -> TokenPair:
    """Tokens tied to a session. Pass `session_id` to keep an existing one (a
    refresh); otherwise a new session is recorded for this browser. `announce`
    emails the user when it is a sign-in from somewhere new."""
    if session_id is None:
        session = await create_session(
            db, user_id=membership.user_id, tenant_id=membership.tenant_id, request=request
        )
        session_id = session.id
        await db.commit()
        if announce:
            user = membership.user or await db.get(User, membership.user_id)
            await alert_new_sign_in(db, user, session)
    return TokenPair(
        access_token=create_access_token(
            user_id=membership.user_id,
            membership_id=membership.id,
            tenant_id=membership.tenant_id,
            session_id=session_id,
        ),
        refresh_token=create_refresh_token(
            user_id=membership.user_id,
            membership_id=membership.id,
            tenant_id=membership.tenant_id,
            session_id=session_id,
        ),
    )


async def _load_active_membership(db: AsyncSession, membership_id: uuid.UUID) -> TenantUser | None:
    result = await db.execute(
        select(TenantUser)
        .where(TenantUser.id == membership_id)
        .options(joinedload(TenantUser.user), joinedload(TenantUser.tenant))
    )
    membership = result.unique().scalar_one_or_none()
    if (
        membership is None
        or membership.status != MembershipStatus.ACTIVE
        or not membership.user.is_active
        or membership.tenant.status not in (TenantStatus.ACTIVE, TenantStatus.TRIAL)
    ):
        return None
    return membership


@router.post(
    "/signup",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_limit_signup)],
)
async def signup(
    payload: SignupRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenPair:
    try:
        _, _, membership = await workspace_signup.create_workspace(
            db,
            company_name=payload.company_name,
            slug=payload.tenant_slug,
            full_name=payload.full_name,
            email=payload.email,
            hashed_password=hash_password(payload.password),
        )
    except workspace_signup.SystemRolesMissing as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "System roles are not seeded — run migrations before accepting signups",
        ) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("tenant_slug or email is already in use") from exc
    await db.commit()

    return await _issue_tokens(db, membership, request)


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(_limit_login)])
async def login(
    payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> LoginResponse:
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if user is None:
        burn_password_check(payload.password)
    valid_password = user is not None and verify_password(payload.password, user.hashed_password)
    if user is None or not user.is_active or not valid_password:
        raise InvalidCredentialsError()

    totp_row = await get_enabled_totp(db, user.id)
    if totp_row is not None:
        if not payload.totp_code:
            return LoginResponse(requires_2fa=True)
        if not await verify_second_factor(db, totp_row, payload.totp_code):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication code")

    query = (
        select(TenantUser)
        .join(Tenant, TenantUser.tenant_id == Tenant.id)
        .where(
            TenantUser.user_id == user.id,
            TenantUser.status == MembershipStatus.ACTIVE,
            Tenant.status.in_([TenantStatus.ACTIVE, TenantStatus.TRIAL]),
        )
        .options(joinedload(TenantUser.tenant), joinedload(TenantUser.role))
    )
    if payload.tenant_slug is not None:
        query = query.where(Tenant.slug == payload.tenant_slug)

    memberships = (await db.execute(query)).unique().scalars().all()

    if not memberships:
        raise InvalidCredentialsError()

    if len(memberships) > 1:
        return LoginResponse(
            requires_tenant_selection=True,
            tenants=[
                TenantOption(
                    tenant_id=m.tenant_id,
                    tenant_name=m.tenant.name,
                    tenant_slug=m.tenant.slug,
                    role_name=m.role.name,
                )
                for m in memberships
            ],
        )

    return LoginResponse(tokens=await _issue_tokens(db, memberships[0], request, announce=True))


@router.post("/refresh", response_model=TokenPair, dependencies=[Depends(_limit_login)])
async def refresh(
    payload: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenPair:
    try:
        token_payload = decode_token(payload.refresh_token, expected_type=TokenType.REFRESH)
    except InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token"
        ) from exc

    membership = await _load_active_membership(db, uuid.UUID(token_payload.membership_id))
    if membership is None or str(membership.user_id) != token_payload.sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session is no longer valid")

    session_id = uuid.UUID(token_payload.sid) if token_payload.sid else None
    if session_id is not None:
        if not await session_is_active(db, session_id, membership.user_id):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This session was signed out")
        await touch(db, session_id)
        await db.commit()
    # A token from before sessions existed gets its first session here.
    return await _issue_tokens(db, membership, request, session_id=session_id)


@router.post("/switch-tenant", response_model=TokenPair)
async def switch_tenant(
    payload: SwitchTenantRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    token_payload=Depends(get_access_token_payload),
) -> TokenPair:
    result = await db.execute(
        select(TenantUser)
        .join(Tenant, TenantUser.tenant_id == Tenant.id)
        .where(
            TenantUser.user_id == uuid.UUID(token_payload.sub),
            Tenant.slug == payload.tenant_slug,
            TenantUser.status == MembershipStatus.ACTIVE,
            Tenant.status.in_([TenantStatus.ACTIVE, TenantStatus.TRIAL]),
        )
        .options(joinedload(TenantUser.tenant), joinedload(TenantUser.user))
    )
    membership = result.unique().scalar_one_or_none()
    if membership is None or not membership.user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of that tenant")

    session_id = uuid.UUID(token_payload.sid) if token_payload.sid else None
    return await _issue_tokens(db, membership, request, session_id=session_id)


@router.post(
    "/forgot-password", response_model=MessageResponse, dependencies=[Depends(_limit_reset)]
)
async def forgot_password(
    payload: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()

    if user is not None and user.is_active:
        token = create_purpose_token(
            user_id=user.id,
            purpose=PASSWORD_RESET_PURPOSE,
            expires_delta=timedelta(minutes=30),
            fingerprint=password_fingerprint(user.hashed_password),
        )
        link = f"{get_settings().frontend_base_url}/reset-password?token={token}"
        try:
            await send_email(
                to_email=user.email,
                subject="Reset your password",
                html_body=f"<p>Reset your password by visiting <a href='{link}'>this link</a>. "
                "This link expires in 30 minutes.</p>",
            )
        except Exception:
            logger.exception("failed to send password reset email to %s", user.email)

    return MessageResponse(message=_FORGOT_PASSWORD_MESSAGE)


@router.post(
    "/reset-password", response_model=MessageResponse, dependencies=[Depends(_limit_reset)]
)
async def reset_password(
    payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    try:
        user_id, fingerprint = decode_purpose_token_full(
            payload.token, expected_purpose=PASSWORD_RESET_PURPOSE
        )
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token") from exc

    user = await db.get(User, user_id)
    # The fingerprint ties the link to the password it was issued for, so it
    # works exactly once: after a reset the stored hash (and fingerprint) change.
    if (
        user is None
        or not user.is_active
        or fingerprint is None
        or fingerprint != password_fingerprint(user.hashed_password)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token")

    user.hashed_password = hash_password(payload.new_password)
    # Whoever knew the old password (or stole a session) is signed out everywhere.
    await revoke_others(db, user.id, keep=None)
    await db.commit()

    return MessageResponse(message="Password has been reset. You can now log in.")


# --- Signed-in sessions -----------------------------------------------------


def _session_read(row, current: uuid.UUID | None) -> SessionRead:
    return SessionRead(
        id=row.id,
        device=describe_device(row.user_agent),
        ip_address=row.ip_address,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        current=row.id == current,
    )


def _humans_only(ctx: RequestContext) -> None:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys have no sign-in sessions")


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> list[SessionRead]:
    """The devices signed in to the caller's own account."""
    _humans_only(ctx)
    return [_session_read(row, ctx.session_id) for row in await list_active(db, ctx.user_id)]


@router.post("/sessions/revoke-others", response_model=RevokedCount)
async def revoke_other_sessions(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> RevokedCount:
    """Sign out everywhere except this browser."""
    _humans_only(ctx)
    count = await revoke_others(db, ctx.user_id, keep=ctx.session_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="session.revoked_others",
        resource_type="user",
        resource_id=ctx.user_id,
        extra={"count": count},
    )
    await db.commit()
    return RevokedCount(revoked=count)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    _humans_only(ctx)
    # A session that isn't yours looks exactly like one that doesn't exist.
    if not await revoke(db, ctx.user_id, session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="session.revoked",
        resource_type="user",
        resource_id=ctx.user_id,
        extra={"session_id": str(session_id), "current": session_id == ctx.session_id},
    )
    await db.commit()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> None:
    """Ends this browser's session on the server, so its tokens stop working
    at once rather than when they expire."""
    if ctx.session_id is not None:
        await revoke(db, ctx.user_id, ctx.session_id)
        await db.commit()
