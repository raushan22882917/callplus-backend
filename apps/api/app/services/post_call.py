"""Messages sent after a call ends: a thank-you SMS and email to the customer,
and a summary email to the team. Each is sent at most once per call, and a
failure of one never stops the others."""

import html
import logging
import math
import re
import uuid
from datetime import UTC, datetime

from app.integrations.email_client import send_email
from app.services import cost_controls, csat
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, CallStatus, Customer, Tenant
from packages.database.models.platform import UsageEvent
from packages.database.models.post_call import CallNotification, PostCallSettings

logger = logging.getLogger(__name__)

SMS_MAX_CHARS = 320
SMS_SEGMENT = 160
MACHINE_ANSWERS = {"machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}
_TOKEN = re.compile(r"\{(business|name|summary)\}")


async def get_settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> PostCallSettings:
    row = (
        await db.execute(select(PostCallSettings).where(PostCallSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = PostCallSettings(tenant_id=tenant_id, staff_emails=[])
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def fill(template: str, *, business: str, name: str, summary: str) -> str:
    """Replaces {business}, {name} and {summary}; any other braces are left alone."""
    values = {"business": business, "name": name, "summary": summary}
    text = _TOKEN.sub(lambda m: values[m.group(1)], template)
    return re.sub(r"\s+", " ", text).strip()


def sms_text(template: str, *, business: str, name: str, summary: str) -> str:
    text = fill(template, business=business, name=name, summary=summary)
    if len(text) <= SMS_MAX_CHARS:
        return text
    return text[: SMS_MAX_CHARS - 1].rstrip() + "…"


def sms_segments(text: str) -> int:
    return max(1, math.ceil(len(text) / SMS_SEGMENT))


async def _claim(
    db: AsyncSession, call: Call, *, channel: str, audience: str, recipient: str
) -> uuid.UUID | None:
    """Reserves this message for this call. None means it was already handled."""
    result = await db.execute(
        pg_insert(CallNotification)
        .values(
            id=uuid.uuid4(),
            tenant_id=call.tenant_id,
            call_id=call.id,
            channel=channel,
            audience=audience,
            recipient=recipient,
            status="pending",
        )
        .on_conflict_do_nothing(constraint="uq_call_notification")
        .returning(CallNotification.id)
    )
    row = result.first()
    await db.commit()
    return row[0] if row else None


async def _finish(db: AsyncSession, row_id: uuid.UUID, status: str, error: str | None = None) -> None:
    row = await db.get(CallNotification, row_id)
    if row is not None:
        row.status = status
        row.error = error[:500] if error else None
        await db.commit()


async def _send_customer_sms(
    db: AsyncSession, call: Call, customer: Customer, settings: PostCallSettings, text: str
) -> None:
    if not customer.phone:
        return
    row_id = await _claim(db, call, channel="sms", audience="customer", recipient=customer.phone)
    if row_id is None:
        return
    if await cost_controls.budget_exhausted(db, call.tenant_id):
        await _finish(db, row_id, "skipped", "Monthly budget used up; paid messages are paused")
        return
    try:
        await csat._send_sms(db, call.tenant_id, customer, text)
    except csat.SurveySendError as exc:
        await _finish(db, row_id, "failed", str(exc))
        return
    except Exception as exc:
        logger.warning("post-call SMS for call %s failed", call.id, exc_info=True)
        await _finish(db, row_id, "failed", str(exc))
        return
    # Counted so SMS shows up in usage and against the monthly budget.
    db.add(
        UsageEvent(
            tenant_id=call.tenant_id,
            metric="sms",
            quantity=sms_segments(text),
            occurred_at=datetime.now(UTC),
            reference_id=call.id,
            extra={"kind": "post_call"},
        )
    )
    await db.commit()
    await _finish(db, row_id, "sent")


async def _send_email_once(
    db: AsyncSession, call: Call, *, audience: str, to: str, subject: str, body: str
) -> None:
    row_id = await _claim(db, call, channel="email", audience=audience, recipient=to)
    if row_id is None:
        return
    try:
        await send_email(to_email=to, subject=subject, html_body=body)
    except Exception as exc:
        logger.warning("post-call email for call %s failed", call.id, exc_info=True)
        await _finish(db, row_id, "failed", str(exc))
        return
    await _finish(db, row_id, "sent")


def _customer_email_body(business: str, name: str, summary: str) -> str:
    greeting = f"Hi {html.escape(name)}," if name else "Hello,"
    recap = f"<p>{html.escape(summary)}</p>" if summary else ""
    return (
        f"<p>{greeting}</p><p>Thank you for calling {html.escape(business)}.</p>{recap}"
        f"<p>If anything above isn't right, just reply to this message or call us again.</p>"
    )


def _staff_email_body(call: Call, customer: Customer | None, summary: str) -> str:
    who = (customer.name if customer else None) or "Unknown caller"
    number = call.from_number if call.direction.value == "inbound" else call.to_number
    minutes = (call.duration_seconds or 0) / 60
    rows = [
        ("Customer", f"{who} ({number or 'no number'})"),
        ("Direction", call.direction.value),
        ("Length", f"{minutes:.1f} min"),
        ("Intent", call.intent or "-"),
        ("Sentiment", call.sentiment or "-"),
    ]
    items = "".join(f"<li>{html.escape(str(i))}</li>" for i in (call.action_items or []))
    table = "".join(f"<tr><td><b>{k}</b></td><td>{html.escape(v)}</td></tr>" for k, v in rows)
    return (
        f"<p>A call just ended.</p><table cellpadding='4'>{table}</table>"
        f"<p>{html.escape(summary) if summary else 'No summary was produced.'}</p>"
        + (f"<p><b>Follow-ups</b></p><ul>{items}</ul>" if items else "")
    )


async def notify_after_call(db: AsyncSession, call_id: uuid.UUID) -> dict:
    """Sends whatever this business has switched on for a finished call. Returns
    what was attempted, e.g. {"sent": 2}; safe to run again for the same call."""
    call = await db.get(Call, call_id)
    if call is None or call.status != CallStatus.COMPLETED:
        return {"skipped": "not_completed"}
    settings = await get_settings_row(db, call.tenant_id)
    await db.commit()
    if not settings.enabled:
        return {"skipped": "disabled"}
    if (call.duration_seconds or 0) < settings.min_call_seconds:
        return {"skipped": "too_short"}
    if call.outcome == "voicemail" or (call.answered_by or "") in MACHINE_ANSWERS:
        return {"skipped": "voicemail"}

    tenant = await db.get(Tenant, call.tenant_id)
    business = tenant.name if tenant else "us"
    customer = await db.get(Customer, call.customer_id) if call.customer_id else None
    name = (customer.name or "").strip() if customer else ""
    summary = (call.summary or "").strip() if settings.include_summary else ""

    if customer is not None and settings.customer_sms:
        text = sms_text(settings.sms_message, business=business, name=name, summary=summary)
        await _send_customer_sms(db, call, customer, settings, text)
    if customer is not None and customer.email and settings.customer_email:
        await _send_email_once(
            db,
            call,
            audience="customer",
            to=customer.email.strip().lower(),
            subject=fill(settings.email_subject, business=business, name=name, summary=summary)
            or f"Thanks for calling {business}",
            body=_customer_email_body(business, name, summary),
        )
    if settings.staff_email:
        staff_body = _staff_email_body(call, customer, (call.summary or "").strip())
        for address in settings.staff_emails or []:
            await _send_email_once(
                db,
                call,
                audience="staff",
                to=address,
                subject=f"Call summary: {customer.name if customer else 'caller'}",
                body=staff_body,
            )

    rows = (
        await db.execute(select(CallNotification.status).where(CallNotification.call_id == call.id))
    ).scalars().all()
    return {s: rows.count(s) for s in set(rows)}
