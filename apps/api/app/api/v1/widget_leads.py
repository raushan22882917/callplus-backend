import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.schemas.widget_leads import WidgetLeadDetail, WidgetLeadRead, WidgetLeadSummary
from app.services import channels as channel_svc
from app.services import widget_leads as svc
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.widget_lead import CALL_STATUSES, WidgetLead

router = APIRouter(prefix="/widget-leads", tags=["widget-leads"])

_READ = Permissions.CUSTOMERS_READ
_MANAGE = Permissions.CUSTOMERS_MANAGE
EXPORT_ROW_LIMIT = 10_000
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _filtered(query, tenant_id: uuid.UUID, *, call_status: str | None, q: str | None, since: datetime | None):
    query = query.where(WidgetLead.tenant_id == tenant_id)
    if call_status:
        query = query.where(WidgetLead.call_status == call_status)
    if q:
        like = f"%{q.strip()[:80]}%"
        query = query.where(
            or_(
                WidgetLead.name.ilike(like, escape="\\"),
                WidgetLead.phone.ilike(like, escape="\\"),
                WidgetLead.email.ilike(like, escape="\\"),
            )
        )
    if since:
        query = query.where(WidgetLead.created_at >= since)
    return query


def _escape_like(text: str | None) -> str | None:
    return None if text is None else text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _get(db: AsyncSession, ctx: RequestContext, lead_id: uuid.UUID) -> WidgetLead:
    lead = await db.get(WidgetLead, lead_id)
    if lead is None or lead.tenant_id != ctx.tenant_id:
        raise NotFoundError("Lead not found")
    return lead


@router.get("", response_model=list[WidgetLeadRead])
async def list_widget_leads(
    call_status: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=80),
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[WidgetLead]:
    if call_status and call_status not in CALL_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown call status")
    query = _filtered(select(WidgetLead), ctx.tenant_id, call_status=call_status, q=_escape_like(q), since=since)
    rows = await db.execute(query.order_by(WidgetLead.created_at.desc()).limit(limit).offset(offset))
    return list(rows.scalars().all())


@router.get("/summary", response_model=WidgetLeadSummary)
async def widget_lead_summary(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> WidgetLeadSummary:
    since = datetime.now(UTC) - timedelta(days=days)
    base = (WidgetLead.tenant_id == ctx.tenant_id, WidgetLead.created_at >= since)

    async def count(*extra) -> int:
        return (await db.execute(select(func.count(WidgetLead.id)).where(*base, *extra))).scalar_one()

    return WidgetLeadSummary(
        days=days,
        leads=await count(),
        with_phone=await count(WidgetLead.phone.is_not(None)),
        call_requests=await count(WidgetLead.call_requested_at.is_not(None)),
        calls_completed=await count(WidgetLead.call_status == "completed"),
        needs_follow_up=await count(WidgetLead.call_status.in_(["no_answer", "failed", "blocked", "requested"])),
    )


@router.get("/export")
async def export_widget_leads(
    call_status: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=80),
    since: datetime | None = None,
    ctx: RequestContext = Depends(require_permission(_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Every captured detail, including consent evidence, as a CSV (up to 10,000 rows)."""
    if ctx.api_key_id is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Exports need a signed-in user")
    query = _filtered(select(WidgetLead), ctx.tenant_id, call_status=call_status, q=_escape_like(q), since=since)
    leads = (await db.execute(query.order_by(WidgetLead.created_at.desc()).limit(EXPORT_ROW_LIMIT))).scalars().all()

    def cell(value: object) -> str:
        text = "" if value is None else str(value)
        return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text

    # One column per question the business ever asked, in the order they first appear.
    questions: list[str] = []
    for lead in leads:
        for answer in lead.answers or []:
            if answer.get("label") not in questions:
                questions.append(answer.get("label"))

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["created_utc", "name", "phone", "email", "interest", *[f"answer: {q}" for q in questions], "page_url",
         "referrer", "utm", "call_consent", "consent_at_utc", "consent_text", "call_status", "call_attempts",
         "last_call_error"]
    )
    for lead in leads:
        by_label = {a.get("label"): a.get("value") for a in lead.answers or []}
        shown = ["" if by_label.get(q) is None else ("Yes" if by_label[q] is True else "No" if by_label[q] is False else by_label[q]) for q in questions]
        writer.writerow(
            [cell(v) for v in (
                lead.created_at.isoformat(), lead.name, lead.phone, lead.email, lead.interest, *shown,
                lead.page_url, lead.referrer, lead.utm or "", "yes" if lead.consent_call else "no",
                lead.consent_at.isoformat() if lead.consent_at else "", lead.consent_text, lead.call_status,
                lead.call_attempts, lead.last_call_error,
            )]
        )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="widget_leads.exported",
        resource_type="widget_lead",
        extra={"rows": len(leads)},
    )
    await db.commit()
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="chat-leads.csv"'},
    )


@router.get("/{lead_id}", response_model=WidgetLeadDetail)
async def get_widget_lead(
    lead_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(_READ)),
    db: AsyncSession = Depends(get_db),
) -> WidgetLead:
    return await _get(db, ctx, lead_id)


@router.post("/{lead_id}/call", response_model=WidgetLeadDetail)
async def call_widget_lead(
    lead_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> WidgetLead:
    """The AI agent calls (or calls back) a lead who agreed to be called."""
    lead = await _get(db, ctx, lead_id)
    row = await channel_svc.get_channel_settings(db, ctx.tenant_id, "web_chat")
    try:
        await svc.call_lead(db, lead, row.ai_agent_id if row else None, dict(row.config or {}) if row else None)
    except svc.LeadError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="widget_lead.called",
        resource_type="widget_lead",
        resource_id=lead.id,
    )
    await db.commit()
    await db.refresh(lead)
    return lead
