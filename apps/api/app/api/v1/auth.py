import logging
import uuid
from datetime import UTC, datetime, timedelta

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
    decode_purpose_token,
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
    SignupRequest,
    SwitchTenantRequest,
    TenantOption,
    TokenPair,
    VerifyEmailRequest,
)
from app.services.platform_totp import get_enabled_totp, verify_second_factor
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from packages.database.models import (
    MembershipStatus,
    Role,
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
EMAIL_VERIFY_PURPOSE = "email_verify"
# Generic response for forgot-password regardless of whether the email
# matched an account — avoids leaking which emails have accounts.
_FORGOT_PASSWORD_MESSAGE = "If an account with that email exists, a reset link has been sent."


async def _send_verification_email(user: User) -> None:
    token = create_purpose_token(
        user_id=user.id, purpose=EMAIL_VERIFY_PURPOSE, expires_delta=timedelta(hours=24)
    )
    link = f"{get_settings().frontend_base_url}/verify-email?token={token}"
    try:
        await send_email(
            to_email=user.email,
            subject="Verify your email",
            html_body=f"<p>Confirm your email by visiting <a href='{link}'>this link</a>.</p>",
        )
    except Exception:
        # Best-effort — signup/resend must not fail because SMTP is down.
        logger.exception("failed to send verification email to %s", user.email)


def _issue_tokens(membership: TenantUser) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(
            user_id=membership.user_id, membership_id=membership.id, tenant_id=membership.tenant_id
        ),
        refresh_token=create_refresh_token(
            user_id=membership.user_id, membership_id=membership.id, tenant_id=membership.tenant_id
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
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    owner_role = (
        await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.name == "OWNER"))
    ).scalar_one_or_none()
    if owner_role is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "System roles are not seeded — run migrations before accepting signups",
        )

    tenant = Tenant(name=payload.company_name, slug=payload.tenant_slug)
    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add_all([tenant, user])
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("tenant_slug or email is already in use") from exc

    membership = TenantUser(tenant_id=tenant.id, user_id=user.id, role_id=owner_role.id)
    db.add(membership)
    await db.flush()
    await db.commit()

    await _send_verification_email(user)

    return _issue_tokens(membership)


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(_limit_login)])
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
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

    return LoginResponse(tokens=_issue_tokens(memberships[0]))


@router.post("/refresh", response_model=TokenPair, dependencies=[Depends(_limit_login)])
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    try:
        token_payload = decode_token(payload.refresh_token, expected_type=TokenType.REFRESH)
    except InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token"
        ) from exc

    membership = await _load_active_membership(db, uuid.UUID(token_payload.membership_id))
    if membership is None or str(membership.user_id) != token_payload.sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session is no longer valid")

    return _issue_tokens(membership)


@router.post("/switch-tenant", response_model=TokenPair)
async def switch_tenant(
    payload: SwitchTenantRequest,
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

    return _issue_tokens(membership)


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
    await db.commit()

    return MessageResponse(message="Password has been reset. You can now log in.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    payload: VerifyEmailRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    try:
        user_id = decode_purpose_token(payload.token, expected_purpose=EMAIL_VERIFY_PURPOSE)
    except InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid or expired verification token"
        ) from exc

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired verification token")

    user.email_verified_at = datetime.now(UTC)
    await db.commit()

    return MessageResponse(message="Email verified.")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot trigger account emails")
    user = await db.get(User, ctx.user_id)
    if user.email_verified_at is not None:
        return MessageResponse(message="Email is already verified.")

    await _send_verification_email(user)
    return MessageResponse(message="Verification email sent.")
