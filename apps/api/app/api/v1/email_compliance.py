"""Unsubscribe, the suppression list, marketing consent, and bounce / complaint events.

Public routes (the unsubscribe link and the provider webhooks) take no login:
the link carries a signed token, and the webhooks are checked against a
secret that must be configured, otherwise they refuse every request.
"""

import base64
import hashlib
import hmac
import html
import json
import time
import uuid

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.rate_limit_ip import ip_rate_limit
from app.services import email_compliance as svc
from app.services import privacy
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant
from packages.database.models.email_suppression import EmailSuppression

router = APIRouter(tags=["email-compliance"])

Read = Depends(require_permission(Permissions.CAMPAIGNS_READ))
Manage = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE))

# People who opted out or complained cannot be put back by staff: that would be mailing them against their word.
_LOCKED = ("unsubscribed", "complaint")


# ---- public: the unsubscribe link ---------------------------------------------------------------


def _page(title: str, message: str, form_action: str | None = None, code: int = 200) -> HTMLResponse:
    form = (
        f'<form method="post" action="{html.escape(form_action, quote=True)}">'
        '<input type="hidden" name="confirm" value="1">'
        '<button type="submit">Unsubscribe</button></form>'
        if form_action
        else ""
    )
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem}"
        "button{font-size:1rem;padding:.6rem 1.2rem}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{form}</body></html>"
    )
    return HTMLResponse(body, status_code=code)


async def _business(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    tenant = await db.get(Tenant, tenant_id)
    return tenant.name if tenant else "this sender"


@router.get(
    "/email/unsubscribe/{token}",
    include_in_schema=False,
    dependencies=[Depends(ip_rate_limit("email-unsubscribe", limit=30))],
)
async def unsubscribe_page(token: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Only shows the button. A mail scanner that opens the link must not unsubscribe anyone."""
    parsed = svc.parse_token(token)
    if parsed is None:
        return _page("Link not valid", "This unsubscribe link is not valid. Reply to the email and we will remove you.", code=404)
    tenant_id, email = parsed
    return _page(
        "Unsubscribe",
        f"Stop marketing email from {await _business(db, tenant_id)} to {email}?",
        form_action=f"/api/v1/email/unsubscribe/{token}",
    )


@router.post(
    "/email/unsubscribe/{token}",
    include_in_schema=False,
    dependencies=[Depends(ip_rate_limit("email-unsubscribe", limit=30))],
)
async def unsubscribe(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """The button, and also the one-click POST (RFC 8058) that Gmail and Yahoo send. Both end here."""
    parsed = svc.parse_token(token)
    if parsed is None:
        return _page("Link not valid", "This unsubscribe link is not valid. Reply to the email and we will remove you.", code=404)
    tenant_id, email = parsed
    one_click = "one-click" in (await request.body()).decode("utf-8", "ignore").lower()
    added = await svc.unsubscribe(db, tenant_id, email, source="one_click" if one_click else "unsubscribe_link")
    if added:
        await log_audit_event(
            db, tenant_id=tenant_id, actor_user_id=None, action="email.unsubscribed",
            resource_type="email_suppression", resource_id=None,
            extra={"via": "one_click" if one_click else "link"},
        )
    await db.commit()
    return _page("You are unsubscribed", f"{await _business(db, tenant_id)} will not send marketing email to {email} again.")


# ---- staff: suppression list --------------------------------------------------------------------


class SuppressionRead(BaseModel):
    id: uuid.UUID
    email: str
    reason: str
    source: str
    detail: str | None
    created_at: str


class SuppressionIn(BaseModel):
    email: EmailStr
    detail: str | None = Field(default=None, max_length=500)


def _row(row: EmailSuppression) -> SuppressionRead:
    return SuppressionRead(
        id=row.id, email=row.email, reason=row.reason, source=row.source, detail=row.detail,
        created_at=row.created_at.isoformat(),
    )


@router.get("/email/suppressions", response_model=list[SuppressionRead])
async def list_suppressions(
    reason: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: RequestContext = Read,
    db: AsyncSession = Depends(get_db),
) -> list[SuppressionRead]:
    stmt = select(EmailSuppression).where(EmailSuppression.tenant_id == ctx.tenant_id)
    if reason:
        stmt = stmt.where(EmailSuppression.reason == reason)
    rows = await db.execute(stmt.order_by(EmailSuppression.created_at.desc()).limit(limit))
    return [_row(r) for r in rows.scalars().all()]


@router.post("/email/suppressions", response_model=SuppressionRead, status_code=status.HTTP_201_CREATED)
async def add_suppression(
    payload: SuppressionIn, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> SuppressionRead:
    await svc.suppress(db, ctx.tenant_id, str(payload.email), reason="manual", source="staff", detail=payload.detail)
    row = await svc.get_suppression(db, ctx.tenant_id, str(payload.email))
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="email_suppression.added",
        resource_type="email_suppression", resource_id=row.id,
    )
    await db.commit()
    return _row(row)


@router.delete("/email/suppressions/{suppression_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_suppression(
    suppression_id: uuid.UUID, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> None:
    row = (
        await db.execute(
            select(EmailSuppression).where(
                EmailSuppression.id == suppression_id, EmailSuppression.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if row.reason in _LOCKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This person unsubscribed or reported spam, so they cannot be put back on the list.",
        )
    await db.delete(row)
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="email_suppression.removed",
        resource_type="email_suppression", resource_id=suppression_id,
    )
    await db.commit()


# ---- staff: marketing consent -------------------------------------------------------------------


class ConsentIn(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=255)


@router.post("/email/consents", status_code=status.HTTP_201_CREATED)
async def record_email_consent(
    payload: ConsentIn, request: Request, ctx: RequestContext = Manage, db: AsyncSession = Depends(get_db)
) -> dict:
    """Staff records that this person agreed to marketing email (a signed form, a sign-up, a call)."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    if await svc.get_suppression(db, ctx.tenant_id, str(payload.email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This address is on the suppression list.")
    rows = await privacy.record_consent(
        db, ctx.tenant_id, purposes=["marketing"], source="staff", business=tenant.name if tenant else "us",
        name=payload.name, email=str(payload.email),
    )
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="email_consent.recorded",
        resource_type="consent_record", resource_id=rows[0].id,
    )
    await db.commit()
    return {"recorded": len(rows)}


# ---- provider events: bounces and complaints ----------------------------------------------------

_SVIX_TOLERANCE_SECONDS = 300


def verify_svix(secret: str, headers, body: bytes, now: float | None = None) -> bool:
    """Resend signs webhooks with Svix: HMAC-SHA256 over "id.timestamp.body", key = base64 of the whsec_ secret."""
    if not secret:
        return False
    msg_id = headers.get("svix-id", "")
    stamp = headers.get("svix-timestamp", "")
    given = headers.get("svix-signature", "")
    if not (msg_id and stamp and given):
        return False
    try:
        if abs((now if now is not None else time.time()) - int(stamp)) > _SVIX_TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    digest = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return any(
        hmac.compare_digest(expected, part.split(",", 1)[1])
        for part in given.split()
        if part.startswith("v1,") and "," in part
    )


def _tenant_from_tags(tags) -> uuid.UUID | None:
    values: list[str] = []
    if isinstance(tags, dict):
        values = [str(tags.get("tenant", ""))]
    elif isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("name") == "tenant":
                values.append(str(tag.get("value", "")))
            elif isinstance(tag, str):
                values.append(tag.removeprefix("tenant_"))
    elif isinstance(tags, str):
        values = [tags.removeprefix("tenant_")]
    for value in values:
        try:
            return uuid.UUID(value)
        except ValueError:
            continue
    return None


async def _apply_event(db: AsyncSession, tenant_id: uuid.UUID | None, emails: list[str], reason: str, source: str, detail: str) -> int:
    if tenant_id is None or await db.get(Tenant, tenant_id) is None:
        return 0  # not sent by this app, so there is nobody to suppress it for
    added = 0
    for address in emails:
        if svc.normalize_email(address) and await svc.suppress(db, tenant_id, address, reason=reason, source=source, detail=detail):
            added += 1
    await db.commit()
    return added


@router.post("/email/events/resend", include_in_schema=False)
async def resend_events(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    body = await request.body()
    if not verify_svix(get_settings().email_resend_webhook_secret, request.headers, body):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signature")
    try:
        event = json.loads(body)
        kind, data = str(event.get("type", "")), event.get("data") or {}
    except (ValueError, AttributeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad payload") from None
    if kind == "email.complained":
        reason = "complaint"
    elif kind == "email.bounced" and str((data.get("bounce") or {}).get("type", "")).lower() != "transient":
        reason = "bounced"
    else:
        return {"suppressed": 0}  # delivered, opened, a soft bounce: nothing to list
    recipients = [data["to"]] if isinstance(data.get("to"), str) else list(data.get("to") or [])
    added = await _apply_event(db, _tenant_from_tags(data.get("tags")), recipients, reason, "provider_resend", kind)
    return {"suppressed": added}


_BREVO_REASONS = {
    "hard_bounce": "bounced",
    "invalid_email": "bounced",
    "blocked": "bounced",
    "spam": "complaint",
    "complaint": "complaint",
    "unsubscribed": "unsubscribed",
}


@router.post("/email/events/brevo", include_in_schema=False)
async def brevo_events(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    token = get_settings().email_brevo_webhook_token
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(given, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid token")
    try:
        event = await request.json()
        kind = str(event.get("event", "")).lower()
    except (ValueError, AttributeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad payload") from None
    reason = _BREVO_REASONS.get(kind)
    if reason is None:
        return {"suppressed": 0}
    tenant_id = _tenant_from_tags(event.get("tags") or event.get("tag"))
    added = await _apply_event(db, tenant_id, [str(event.get("email", ""))], reason, "provider_brevo", kind)
    return {"suppressed": added}
