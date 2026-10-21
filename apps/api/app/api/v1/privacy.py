"""DPDP Act controls: the business's privacy notice and officer, the consent
ledger, requests from people about their data, and the breach register.
Staff routes need settings permission; /privacy/public/{slug} routes are for the
people whose data it is, so they are unauthenticated and rate limited."""

import uuid
from datetime import UTC, datetime

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.rate_limit_ip import ip_rate_limit
from app.schemas.privacy import (
    BreachCreate,
    BreachRead,
    BreachUpdate,
    Checklist,
    ConsentRead,
    ProfileRead,
    ProfileUpdate,
    PublicRequestBody,
    PublicStatusBody,
    RequestCreate,
    RequestRead,
    RequestResolve,
    WithdrawBody,
)
from app.services import privacy as svc
from app.services.platform_data import build_export_zip, collect_customer_data, erase_customer
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import ConsentRecord, DataBreach, PrivacyRequest, Tenant

router = APIRouter(prefix="/privacy", tags=["privacy"])

Manage = Depends(require_permission(Permissions.SETTINGS_MANAGE))
_OPEN = ("received", "verifying", "in_progress")


def _client_ip(request: Request) -> str:
    if get_settings().trust_x_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _request_read(row: PrivacyRequest) -> RequestRead:
    out = RequestRead.model_validate(row)
    out.state = svc.request_state(row)
    return out


def _breach_read(row: DataBreach) -> BreachRead:
    out = BreachRead.model_validate(row)
    out.state = svc.breach_state(row)
    return out


async def _profile_read(db: AsyncSession, tenant_id: uuid.UUID) -> ProfileRead:
    profile = await svc.get_profile(db, tenant_id)
    tenant = await db.get(Tenant, tenant_id)
    out = ProfileRead.model_validate(profile)
    out.notice_lines = svc.notice_lines(profile, tenant.name if tenant else "us")
    out.public_url = svc.public_url(tenant.slug) if tenant else ""
    return out


# ---- staff: profile and checklist ---------------------------------------------------------------


@router.get("/profile", response_model=ProfileRead)
async def get_profile(ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> ProfileRead:
    out = await _profile_read(db, ctx.tenant_id)
    await db.commit()
    return out


@router.put("/profile", response_model=ProfileRead)
async def update_profile(
    payload: ProfileUpdate, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> ProfileRead:
    await svc.update_profile(db, ctx.tenant_id, payload.model_dump())
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.profile_updated",
        resource_type="tenant", resource_id=ctx.tenant_id,
    )
    out = await _profile_read(db, ctx.tenant_id)
    await db.commit()
    return out


@router.get("/checklist", response_model=Checklist)
async def checklist(ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> dict:
    out = await svc.compliance_checklist(db, ctx.tenant_id)
    await db.commit()
    return out


# ---- staff: consent -------------------------------------------------------------------------


@router.get("/consents/summary")
async def consents_summary(ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> dict:
    return await svc.consent_summary(db, ctx.tenant_id)


@router.get("/consents", response_model=list[ConsentRead])
async def list_consents(
    phone: str | None = Query(default=None, max_length=32),
    email: str | None = Query(default=None, max_length=254),
    status_: str | None = Query(default=None, alias="status", pattern="^(granted|withdrawn)$"),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Manage,
    db: AsyncSession = Depends(get_db),
) -> list[ConsentRecord]:
    stmt = select(ConsentRecord).where(ConsentRecord.tenant_id == ctx.tenant_id)
    if phone:
        stmt = stmt.where(ConsentRecord.subject_phone == svc.normalize_phone(phone))
    if email:
        stmt = stmt.where(ConsentRecord.subject_email == email.strip().lower())
    if status_:
        stmt = stmt.where(ConsentRecord.status == status_)
    rows = await db.execute(stmt.order_by(ConsentRecord.granted_at.desc()).limit(limit))
    return list(rows.scalars().all())


@router.post("/consents/withdraw")
async def withdraw(
    payload: WithdrawBody, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> dict:
    """Staff recording a withdrawal someone made by phone or in person."""
    try:
        result = await svc.withdraw_consent(
            db, ctx.tenant_id, phone=payload.phone, email=payload.email,
            purposes=payload.purposes, via="staff",
        )
    except svc.PrivacyError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.consent_withdrawn",
        resource_type="tenant", resource_id=ctx.tenant_id, extra=result,
    )
    await db.commit()
    return result


# ---- staff: requests ------------------------------------------------------------------------


async def _get_request(db: AsyncSession, tenant_id: uuid.UUID, request_id: uuid.UUID) -> PrivacyRequest:
    row = (
        await db.execute(
            select(PrivacyRequest).where(PrivacyRequest.id == request_id, PrivacyRequest.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    return row


@router.get("/requests", response_model=list[RequestRead])
async def list_requests(
    open_only: bool = False,
    ctx: RequestContext = Manage,
    db: AsyncSession = Depends(get_db),
) -> list[RequestRead]:
    stmt = select(PrivacyRequest).where(PrivacyRequest.tenant_id == ctx.tenant_id)
    if open_only:
        stmt = stmt.where(PrivacyRequest.status.in_(_OPEN))
    rows = (await db.execute(stmt.order_by(PrivacyRequest.due_at).limit(500))).scalars().all()
    return [_request_read(r) for r in rows]


@router.post("/requests", response_model=RequestRead, status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: RequestCreate, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> RequestRead:
    """Staff logging a request that arrived by phone, email or post."""
    try:
        row = await svc.create_request(
            db, ctx.tenant_id, kind=payload.kind, name=payload.name, email=payload.email,
            phone=payload.phone, details=payload.details,
        )
    except svc.PrivacyError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.request_logged",
        resource_type="privacy_request", resource_id=row.id, extra={"kind": row.kind},
    )
    await db.commit()
    return _request_read(row)


@router.post("/requests/{request_id}/verify", response_model=RequestRead)
async def verify_request(
    request_id: uuid.UUID, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> RequestRead:
    """The officer confirms the person is who they say they are (by calling the number
    or writing to the address we already hold), before any data is shown or removed."""
    row = await _get_request(db, ctx.tenant_id, request_id)
    if row.status in ("completed", "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This request is already closed")
    row.verified_at = datetime.now(UTC)
    row.verified_by_user_id = ctx.user_id
    row.status = "in_progress"
    row.handled_by_user_id = ctx.user_id
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.request_verified",
        resource_type="privacy_request", resource_id=row.id,
    )
    await db.commit()
    return _request_read(row)


def _require_verified(row: PrivacyRequest) -> None:
    if row.verified_at is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Confirm who is asking before their data is shown or removed"
        )


@router.get("/requests/{request_id}/export")
async def export_for_request(
    request_id: uuid.UUID, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> Response:
    row = await _get_request(db, ctx.tenant_id, request_id)
    _require_verified(row)
    if row.customer_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No stored details match this person")
    data = await collect_customer_data(db, ctx.tenant_id, row.customer_id)
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.request_exported",
        resource_type="privacy_request", resource_id=row.id,
    )
    await db.commit()
    return Response(
        build_export_zip(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{row.reference}.zip"'},
    )


@router.post("/requests/{request_id}/fulfil", response_model=RequestRead)
async def fulfil_request(
    request_id: uuid.UUID,
    payload: RequestResolve | None = None,
    ctx: RequestContext = Manage,
    db: AsyncSession = Depends(get_db),
) -> RequestRead:
    """Does what the request asks and closes it. Erasure and withdrawal act on the
    data; access, correction, grievance and nominee requests are closed with the
    officer's note after the officer has dealt with them."""
    row = await _get_request(db, ctx.tenant_id, request_id)
    if row.status in ("completed", "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This request is already closed")
    _require_verified(row)
    note = payload.resolution if payload else None
    result: dict = {}
    if row.kind == "erasure":
        if row.customer_id is not None:
            result = await erase_customer(db, ctx.tenant_id, row.customer_id)
        else:
            # Nothing stored as a customer; still remove enquiries and consent identity by contact.
            result = await svc.erase_by_contact(db, ctx.tenant_id, row.subject_phone, row.subject_email)
        note = note or "Personal data erased."
        if result.get("recordings_failed"):
            note += f" {result['recordings_failed']} call recording(s) could not be deleted at the provider; retry or delete them there."
    elif row.kind == "withdraw_consent":
        result = await svc.withdraw_consent(
            db, ctx.tenant_id, phone=row.subject_phone, email=row.subject_email, via="request"
        )
        note = note or "Consent withdrawn; we will not use these details for those purposes."
    elif not note:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Write what you did, so the person can be told.")
    row.status = "completed"
    row.completed_at = datetime.now(UTC)
    row.resolution = note
    row.handled_by_user_id = ctx.user_id
    row.customer_id = None if row.kind == "erasure" else row.customer_id
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.request_completed",
        resource_type="privacy_request", resource_id=row.id, extra={"kind": row.kind, **result},
    )
    await db.commit()
    return _request_read(row)


@router.post("/requests/{request_id}/reject", response_model=RequestRead)
async def reject_request(
    request_id: uuid.UUID,
    payload: RequestResolve,
    ctx: RequestContext = Manage,
    db: AsyncSession = Depends(get_db),
) -> RequestRead:
    row = await _get_request(db, ctx.tenant_id, request_id)
    if row.status in ("completed", "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This request is already closed")
    row.status = "rejected"
    row.completed_at = datetime.now(UTC)
    row.resolution = payload.resolution
    row.handled_by_user_id = ctx.user_id
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.request_rejected",
        resource_type="privacy_request", resource_id=row.id,
    )
    await db.commit()
    return _request_read(row)


# ---- staff: breach register -----------------------------------------------------------------


@router.get("/breaches", response_model=list[BreachRead])
async def list_breaches(ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)) -> list[BreachRead]:
    rows = (
        await db.execute(
            select(DataBreach).where(DataBreach.tenant_id == ctx.tenant_id).order_by(DataBreach.discovered_at.desc())
        )
    ).scalars().all()
    return [_breach_read(r) for r in rows]


@router.post("/breaches", response_model=BreachRead, status_code=status.HTTP_201_CREATED)
async def create_breach(
    payload: BreachCreate, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> BreachRead:
    row = await svc.log_breach(db, ctx.tenant_id, user_id=ctx.user_id, **payload.model_dump())
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.breach_logged",
        resource_type="data_breach", resource_id=row.id,
    )
    await db.commit()
    return _breach_read(row)


async def _get_breach(db: AsyncSession, tenant_id: uuid.UUID, breach_id: uuid.UUID) -> DataBreach:
    row = (
        await db.execute(
            select(DataBreach).where(DataBreach.id == breach_id, DataBreach.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Breach not found")
    return row


@router.patch("/breaches/{breach_id}", response_model=BreachRead)
async def update_breach(
    breach_id: uuid.UUID,
    payload: BreachUpdate,
    ctx: RequestContext = Manage,
    db: AsyncSession = Depends(get_db),
) -> BreachRead:
    row = await _get_breach(db, ctx.tenant_id, breach_id)
    now = datetime.now(UTC)
    data = payload.model_dump(exclude_unset=True)
    for key in ("likely_impact", "mitigation", "approx_people", "board_reference", "principals_notified_how"):
        if key in data:
            setattr(row, key, data[key])
    if data.get("board_notified") and not row.board_notified_at:
        row.board_notified_at = now
        row.status = "reported"
    if data.get("principals_notified") and not row.principals_notified_at:
        row.principals_notified_at = now
    if data.get("closed"):
        row.status = "closed"
        row.closed_at = now
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="privacy.breach_updated",
        resource_type="data_breach", resource_id=row.id, extra={k: v for k, v in data.items() if isinstance(v, bool)},
    )
    await db.commit()
    return _breach_read(row)


@router.get("/breaches/{breach_id}/draft")
async def breach_draft(
    breach_id: uuid.UUID, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await _get_breach(db, ctx.tenant_id, breach_id)
    profile = await svc.get_profile(db, ctx.tenant_id)
    tenant = await db.get(Tenant, ctx.tenant_id)
    await db.commit()
    return svc.breach_notice_draft(row, profile, tenant.name if tenant else "us")


# ---- public: for the people the data is about ---------------------------------------------------


async def _tenant_by_slug(db: AsyncSession, slug: str) -> Tenant:
    tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return tenant


@router.get("/public/{slug}/notice", dependencies=[Depends(ip_rate_limit("privacy-notice", limit=60))])
async def public_notice(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant_by_slug(db, slug)
    notice = await svc.public_notice(db, tenant.id, tenant.name)
    await db.commit()
    return notice


@router.post(
    "/public/{slug}/requests",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ip_rate_limit("privacy-request", limit=5))],
)
async def public_request(
    slug: str, payload: PublicRequestBody, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    tenant = await _tenant_by_slug(db, slug)
    if payload.website:  # bot trap: pretend it worked
        return {"reference": "PR-00000000", "due_at": None}
    try:
        row = await svc.create_request(
            db, tenant.id, kind=payload.kind, name=payload.name, email=payload.email,
            phone=payload.phone, details=payload.details, ip=_client_ip(request),
        )
    except svc.PrivacyError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    await log_audit_event(
        db, tenant_id=tenant.id, actor_user_id=None, action="privacy.request_received",
        resource_type="privacy_request", resource_id=row.id, extra={"kind": row.kind},
    )
    await db.commit()
    return {"reference": row.reference, "due_at": row.due_at.isoformat()}


@router.post("/public/{slug}/requests/status", dependencies=[Depends(ip_rate_limit("privacy-status", limit=10))])
async def public_request_status(
    slug: str, payload: PublicStatusBody, db: AsyncSession = Depends(get_db)
) -> dict:
    tenant = await _tenant_by_slug(db, slug)
    row = await svc.request_by_reference(db, tenant.id, payload.reference)
    contact = payload.contact.strip().lower()
    phone = svc.normalize_phone(contact) if any(c.isdigit() for c in contact) else None
    known = row is not None and (contact == (row.subject_email or "") or (phone and phone == row.subject_phone))
    if not known:
        # Same answer whether the reference exists or not.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find a request with those details")
    return {
        "reference": row.reference,
        "kind": row.kind,
        "status": row.status,
        "due_at": row.due_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "resolution": row.resolution,
    }


@router.post(
    "/public/{slug}/withdraw", dependencies=[Depends(ip_rate_limit("privacy-withdraw", limit=5))]
)
async def public_withdraw(
    slug: str, payload: WithdrawBody, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Takes effect at once, so it is as easy to withdraw as it was to agree."""
    tenant = await _tenant_by_slug(db, slug)
    try:
        result = await svc.withdraw_consent(
            db, tenant.id, phone=payload.phone, email=payload.email,
            purposes=payload.purposes, via="public_page",
        )
    except svc.PrivacyError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    await log_audit_event(
        db, tenant_id=tenant.id, actor_user_id=None, action="privacy.consent_withdrawn",
        resource_type="tenant", resource_id=tenant.id, extra={**result, "ip": _client_ip(request)},
    )
    await db.commit()
    return {"ok": True}
