"""Platform module: security (2FA, IP allowlist, PII masking, retention, SSO),
data export/erasure, plus sub-routers for usage/pricing, super-admin and the
integrations hub (see platform_usage.py, platform_admin.py,
platform_connectors.py). All mounted under /platform."""

import uuid
from urllib.parse import quote, urlencode

import httpx
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import RequestContext, get_current_context, get_db, require_permission
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.ip_allowlist import (
    client_ip,
    invalidate_allowlist_cache,
    ip_allowed,
    normalize_entry,
)
from app.core.permissions import Permissions
from app.core.security import create_access_token, create_refresh_token, verify_password
from app.core.totp import (
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    provisioning_uri,
    verify_totp,
)
from app.schemas.platform import (
    DataExportRequest,
    ErasureResult,
    RetentionPurgeResult,
    RetentionRead,
    RetentionUpdate,
    SecuritySettingsRead,
    SecuritySettingsUpdate,
    SsoConfigRead,
    SsoConfigWrite,
    TwoFactorCodeRequest,
    TwoFactorDisableRequest,
    TwoFactorEnrollResponse,
    TwoFactorStatus,
    TwoFactorVerifyResponse,
)
from app.services import platform_sso as sso
from app.services.platform_data import build_export_zip, collect_customer_data, erase_customer
from app.services.platform_retention import purge_tenant
from app.services.platform_totp import get_totp, mark_confirmed, verify_second_factor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant, User
from packages.database.models.platform import (
    RetentionPolicy,
    SsoConfig,
    TenantSecuritySettings,
    UserTotp,
)

router = APIRouter(prefix="/platform", tags=["platform"])

SSO_COOKIE = "sso_nonce"


def _no_api_keys(ctx: RequestContext) -> None:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot manage account security")


# ---- 2FA ----------------------------------------------------------------------


@router.get("/security/2fa", response_model=TwoFactorStatus)
async def two_factor_status(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> TwoFactorStatus:
    _no_api_keys(ctx)
    row = await get_totp(db, ctx.user_id)
    return TwoFactorStatus(
        enabled=bool(row and row.enabled),
        pending_enrollment=bool(row and not row.enabled),
        recovery_codes_remaining=len(row.recovery_code_hashes) if row and row.enabled else 0,
    )


@router.post("/security/2fa/enroll", response_model=TwoFactorEnrollResponse)
async def two_factor_enroll(
    ctx: RequestContext = Depends(get_current_context), db: AsyncSession = Depends(get_db)
) -> TwoFactorEnrollResponse:
    _no_api_keys(ctx)
    row = await get_totp(db, ctx.user_id)
    if row is not None and row.enabled:
        raise ConflictError("Two-factor authentication is already enabled")
    secret = generate_secret()
    if row is None:
        row = UserTotp(user_id=ctx.user_id, encrypted_secret=encrypt_secret(secret))
        db.add(row)
    else:
        row.encrypted_secret = encrypt_secret(secret)
    user = await db.get(User, ctx.user_id)
    await db.commit()
    return TwoFactorEnrollResponse(
        secret=secret,
        otpauth_uri=provisioning_uri(secret, account=user.email, issuer="AutoNXT"),
    )


@router.post("/security/2fa/verify", response_model=TwoFactorVerifyResponse)
async def two_factor_verify(
    payload: TwoFactorCodeRequest,
    ctx: RequestContext = Depends(get_current_context),
    db: AsyncSession = Depends(get_db),
) -> TwoFactorVerifyResponse:
    """Confirms enrollment with a first valid code, turning 2FA on, and
    returns the one-time recovery codes (shown only now)."""
    _no_api_keys(ctx)
    row = await get_totp(db, ctx.user_id)
    if row is None or row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "No pending 2FA enrollment")
    step = verify_totp(decrypt_secret(row.encrypted_secret), payload.code)
    if step is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid code")
    codes = generate_recovery_codes()
    row.recovery_code_hashes = [hash_recovery_code(c) for c in codes]
    row.last_used_step = step
    mark_confirmed(row)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="2fa.enabled",
        resource_type="user",
        resource_id=ctx.user_id,
    )
    await db.commit()
    return TwoFactorVerifyResponse(enabled=True, recovery_codes=codes)


@router.post("/security/2fa/disable", response_model=TwoFactorStatus)
async def two_factor_disable(
    payload: TwoFactorDisableRequest,
    ctx: RequestContext = Depends(get_current_context),
    db: AsyncSession = Depends(get_db),
) -> TwoFactorStatus:
    _no_api_keys(ctx)
    row = await get_totp(db, ctx.user_id)
    if row is None or not row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-factor authentication is not enabled")
    user = await db.get(User, ctx.user_id)
    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incorrect password")
    if not await verify_second_factor(db, row, payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid code")
    await db.delete(row)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="2fa.disabled",
        resource_type="user",
        resource_id=ctx.user_id,
    )
    await db.commit()
    return TwoFactorStatus(enabled=False, pending_enrollment=False, recovery_codes_remaining=0)


@router.post("/security/2fa/recovery-codes", response_model=TwoFactorVerifyResponse)
async def two_factor_regenerate_codes(
    payload: TwoFactorCodeRequest,
    ctx: RequestContext = Depends(get_current_context),
    db: AsyncSession = Depends(get_db),
) -> TwoFactorVerifyResponse:
    _no_api_keys(ctx)
    row = await get_totp(db, ctx.user_id)
    if row is None or not row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-factor authentication is not enabled")
    if not await verify_second_factor(db, row, payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid code")
    codes = generate_recovery_codes()
    row.recovery_code_hashes = [hash_recovery_code(c) for c in codes]
    await db.commit()
    return TwoFactorVerifyResponse(enabled=True, recovery_codes=codes)


# ---- tenant security settings ----------------------------------------------------


async def _security_row(db: AsyncSession, tenant_id) -> TenantSecuritySettings:
    row = (
        await db.execute(
            select(TenantSecuritySettings).where(TenantSecuritySettings.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantSecuritySettings(tenant_id=tenant_id, ip_allowlist=[])
        db.add(row)
        await db.flush()
    return row


def _security_read(row: TenantSecuritySettings, request: Request) -> SecuritySettingsRead:
    return SecuritySettingsRead(
        ip_allowlist=list(row.ip_allowlist or []),
        mask_pii=row.mask_pii,
        mask_phone_non_admin=row.mask_phone_non_admin,
        your_ip=client_ip(request),
    )


@router.get("/security/settings", response_model=SecuritySettingsRead)
async def get_security_settings(
    request: Request,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SecuritySettingsRead:
    row = await _security_row(db, ctx.tenant_id)
    await db.commit()
    return _security_read(row, request)


@router.put("/security/settings", response_model=SecuritySettingsRead)
async def update_security_settings(
    payload: SecuritySettingsUpdate,
    request: Request,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SecuritySettingsRead:
    row = await _security_row(db, ctx.tenant_id)
    if payload.ip_allowlist is not None:
        try:
            entries = sorted({normalize_entry(e) for e in payload.ip_allowlist if e.strip()})
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Invalid IP address or CIDR range: {exc}"
            ) from exc
        me = client_ip(request)
        if entries and not ctx.is_platform_admin and (me is None or not ip_allowed(me, entries)):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Your current IP ({me}) is not in this list; saving it would lock you out",
            )
        row.ip_allowlist = entries
    if payload.mask_pii is not None:
        row.mask_pii = payload.mask_pii
    if payload.mask_phone_non_admin is not None:
        row.mask_phone_non_admin = payload.mask_phone_non_admin
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="security.settings_updated",
        resource_type="tenant",
        resource_id=ctx.tenant_id,
        extra=payload.model_dump(exclude_unset=True),
    )
    await db.commit()
    invalidate_allowlist_cache(ctx.tenant_id)
    return _security_read(row, request)


# ---- retention -----------------------------------------------------------------


async def _retention_row(db: AsyncSession, tenant_id) -> RetentionPolicy:
    row = (
        await db.execute(select(RetentionPolicy).where(RetentionPolicy.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = RetentionPolicy(tenant_id=tenant_id, enabled=False, last_purge_summary={})
        db.add(row)
        await db.flush()
    return row


@router.get("/security/retention", response_model=RetentionRead)
async def get_retention(
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> RetentionPolicy:
    row = await _retention_row(db, ctx.tenant_id)
    await db.commit()
    return row


@router.put("/security/retention", response_model=RetentionRead)
async def update_retention(
    payload: RetentionUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> RetentionPolicy:
    if (
        payload.enabled
        and not payload.transcript_days
        and not payload.recording_days
        and not payload.lead_days
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set transcript_days, recording_days and/or lead_days before enabling retention",
        )
    row = await _retention_row(db, ctx.tenant_id)
    row.enabled = payload.enabled
    row.transcript_days = payload.transcript_days
    row.recording_days = payload.recording_days
    row.lead_days = payload.lead_days
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="retention.updated",
        resource_type="tenant",
        resource_id=ctx.tenant_id,
        extra=payload.model_dump(),
    )
    await db.commit()
    return row


@router.post("/security/retention/purge", response_model=RetentionPurgeResult)
async def run_retention_now(
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Runs this tenant's policy immediately (the daily Celery job does the same)."""
    row = await _retention_row(db, ctx.tenant_id)
    if not row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Retention policy is not enabled")
    result = await purge_tenant(db, row)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="retention.purged",
        resource_type="tenant",
        resource_id=ctx.tenant_id,
        extra=result,
    )
    await db.commit()
    return result


# ---- data export / erasure ----------------------------------------------------------


@router.post("/data/export")
async def export_customer_data(
    payload: DataExportRequest,
    ctx: RequestContext = Depends(
        require_permission(Permissions.SETTINGS_MANAGE, Permissions.CUSTOMERS_MANAGE)
    ),
    db: AsyncSession = Depends(get_db),
) -> Response:
    data = await collect_customer_data(db, ctx.tenant_id, payload.customer_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="customer.data_exported",
        resource_type="customer",
        resource_id=payload.customer_id,
        extra={k: len(v) for k, v in data.items()},
    )
    await db.commit()
    return Response(
        content=build_export_zip(data),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="customer-{payload.customer_id}.zip"'
        },
    )


@router.delete("/data/customers/{customer_id}", response_model=ErasureResult)
async def erase_customer_data(
    customer_id: uuid.UUID,
    ctx: RequestContext = Depends(
        require_permission(Permissions.SETTINGS_MANAGE, Permissions.CUSTOMERS_MANAGE)
    ),
    db: AsyncSession = Depends(get_db),
) -> ErasureResult:
    cid = customer_id
    counts = await erase_customer(db, ctx.tenant_id, cid)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="customer.erased",
        resource_type="customer",
        resource_id=cid,
        extra={"deleted": counts},
    )
    await db.commit()
    return ErasureResult(customer_id=cid, deleted=counts)


# ---- SSO (OIDC) -------------------------------------------------------------------


def _sso_read(config: SsoConfig, slug: str) -> SsoConfigRead:
    return SsoConfigRead(
        enabled=config.enabled,
        issuer=config.issuer,
        client_id=config.client_id,
        has_client_secret=bool(config.encrypted_client_secret),
        default_role_name=config.default_role_name,
        allowed_email_domains=list(config.allowed_email_domains or []),
        login_url=f"{get_settings().public_base_url.rstrip('/')}/api/v1/platform/sso/{slug}/login",
        redirect_uri=sso.redirect_uri(slug),
    )


async def _tenant_slug(db: AsyncSession, tenant_id) -> str:
    return (await db.get(Tenant, tenant_id)).slug


@router.get("/sso/config", response_model=SsoConfigRead | None)
async def get_sso_config(
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SsoConfigRead | None:
    config = (
        await db.execute(select(SsoConfig).where(SsoConfig.tenant_id == ctx.tenant_id))
    ).scalar_one_or_none()
    return _sso_read(config, await _tenant_slug(db, ctx.tenant_id)) if config else None


@router.put("/sso/config", response_model=SsoConfigRead)
async def put_sso_config(
    payload: SsoConfigWrite,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SsoConfigRead:
    config = (
        await db.execute(select(SsoConfig).where(SsoConfig.tenant_id == ctx.tenant_id))
    ).scalar_one_or_none()
    if config is None:
        if not payload.client_secret:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "client_secret is required")
        config = SsoConfig(
            tenant_id=ctx.tenant_id,
            issuer=payload.issuer.rstrip("/"),
            client_id=payload.client_id,
            encrypted_client_secret=encrypt_secret(payload.client_secret),
        )
        db.add(config)
    config.enabled = payload.enabled
    config.issuer = payload.issuer.rstrip("/")
    config.client_id = payload.client_id
    if payload.client_secret:
        config.encrypted_client_secret = encrypt_secret(payload.client_secret)
    config.default_role_name = payload.default_role_name
    config.allowed_email_domains = [
        d.strip().lower() for d in payload.allowed_email_domains if d.strip()
    ]
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="sso.configured",
        resource_type="tenant",
        resource_id=ctx.tenant_id,
        extra={"issuer": config.issuer, "enabled": config.enabled},
    )
    await db.commit()
    return _sso_read(config, await _tenant_slug(db, ctx.tenant_id))


@router.delete("/sso/config", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sso_config(
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    config = (
        await db.execute(select(SsoConfig).where(SsoConfig.tenant_id == ctx.tenant_id))
    ).scalar_one_or_none()
    if config is None:
        raise NotFoundError("SSO is not configured")
    await db.delete(config)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="sso.removed",
        resource_type="tenant",
        resource_id=ctx.tenant_id,
    )
    await db.commit()


def http_client() -> httpx.AsyncClient:
    """Factory so tests can substitute a mock transport."""
    return httpx.AsyncClient(follow_redirects=False)


@router.get("/sso/{tenant_slug}/status")
async def sso_status(tenant_slug: str, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    """Public: lets the login page know whether to offer a 'Sign in with SSO' button."""
    try:
        await sso.load_config(db, tenant_slug)
    except sso.SsoError:
        return {"enabled": False}
    return {"enabled": True}


@router.get("/sso/{tenant_slug}/login")
async def sso_login(tenant_slug: str, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    try:
        _, config = await sso.load_config(db, tenant_slug)
    except sso.SsoError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    async with http_client() as client:
        try:
            discovery = await sso.discover(config.issuer, client)
        except sso.SsoError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    nonce = sso.new_nonce()
    url = sso.build_authorize_url(
        discovery, config, tenant_slug, sso.make_state(tenant_slug, nonce), nonce
    )
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        SSO_COOKIE,
        nonce,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=get_settings().public_base_url.startswith("https://"),
        path=f"/api/v1/platform/sso/{tenant_slug}",
    )
    return response


def _login_error(message: str) -> RedirectResponse:
    base = get_settings().frontend_base_url.rstrip("/")
    return RedirectResponse(f"{base}/login?{urlencode({'sso_error': message})}", status_code=302)


@router.get("/sso/{tenant_slug}/callback")
async def sso_callback(
    tenant_slug: str,
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    try:
        if error:
            raise sso.SsoError(f"Identity provider returned an error: {error}")
        if not code or not state:
            raise sso.SsoError("Missing authorization code or state")
        tenant, config = await sso.load_config(db, tenant_slug)
        payload = sso.read_state(state)
        cookie_nonce = request.cookies.get(SSO_COOKIE)
        if (
            payload.get("tenant") != tenant_slug
            or not cookie_nonce
            or cookie_nonce != payload.get("nonce")
        ):
            raise sso.SsoError("SSO session mismatch; start sign-in again")
        async with http_client() as client:
            discovery = await sso.discover(config.issuer, client)
            tokens = await sso.exchange_code(discovery, config, tenant_slug, code, client)
            claims = await sso.verify_id_token(
                tokens["id_token"],
                discovery=discovery,
                issuer=config.issuer,
                client_id=config.client_id,
                nonce=payload["nonce"],
                client=client,
            )
        membership = await sso.provision_user(db, tenant, config, claims)
        await log_audit_event(
            db,
            tenant_id=tenant.id,
            actor_user_id=membership.user_id,
            action="sso.login",
            resource_type="user",
            resource_id=membership.user_id,
        )
        await db.commit()
    except sso.SsoError as exc:
        return _login_error(str(exc))
    fragment = urlencode(
        {
            "access_token": create_access_token(
                user_id=membership.user_id, membership_id=membership.id, tenant_id=tenant.id
            ),
            "refresh_token": create_refresh_token(
                user_id=membership.user_id, membership_id=membership.id, tenant_id=tenant.id
            ),
        },
        quote_via=quote,
    )
    response = RedirectResponse(
        f"{get_settings().frontend_base_url.rstrip('/')}/sso/callback#{fragment}", status_code=302
    )
    response.delete_cookie(SSO_COOKIE, path=f"/api/v1/platform/sso/{tenant_slug}")
    return response


# Sub-routers (imported last: they import helpers from this package).
from app.api.v1 import platform_admin, platform_connectors, platform_usage  # noqa: E402

router.include_router(platform_usage.router)
router.include_router(platform_admin.router)
router.include_router(platform_connectors.router)
