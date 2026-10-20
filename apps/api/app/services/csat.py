"""CSAT surveys: schedule one after a finished call or resolved ticket, send it
over WhatsApp or SMS, capture the customer's 1-5 reply, and summarise results.

Flow: finalize_call / ticket resolution -> schedule_survey (a pending row due
after `delay_minutes`) -> send_due_surveys (Celery beat, every minute) -> the
customer replies "4 great service" -> channels.process_inbound_text hands the
text to capture_reply before any AI sees it.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from app.services.webhooks import enqueue_webhook_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    Ticket,
    TicketPriority,
    WhatsAppAccount,
)
from packages.database.models.csat import CsatSettings, CsatSurvey

logger = logging.getLogger(__name__)

CHANNELS = ("auto", "whatsapp", "sms")
SOURCES = ("call", "ticket")
BATCH_SIZE = 50
_RATING = re.compile(r"^\s*([1-5])(?![0-9])\s*[-:,.!)]*\s*(.*)$", re.DOTALL)


class SurveySendError(Exception):
    pass


async def get_settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> CsatSettings:
    row = (
        await db.execute(select(CsatSettings).where(CsatSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = CsatSettings(tenant_id=tenant_id)
        db.add(row)
        await db.flush()
        # column defaults apply on flush; refresh so a fresh row reads back fully
        await db.refresh(row)
    return row


def parse_rating(text: str) -> tuple[int, str | None] | None:
    """"4", "4 - fast help", "5!" -> (rating, comment). A bare 1-5 optionally
    followed by a comment; "40" and "hello 4" are not ratings. A message such as
    "3 pm works" does parse as a 3: surveys are only open for a short window, so
    a lone leading digit is far more likely an answer than something else."""
    match = _RATING.match(text or "")
    if match is None:
        return None
    comment = match.group(2).strip()
    return int(match.group(1)), (comment[:1000] or None)


async def schedule_survey(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    source_type: str,
    source_id: uuid.UUID,
    call_duration_seconds: int | None = None,
) -> CsatSurvey | None:
    """Creates the pending survey unless surveys are off, the customer has no
    phone, the source was already surveyed, or they were surveyed recently."""
    if customer_id is None or source_type not in SOURCES:
        return None
    settings = await get_settings_row(db, tenant_id)
    if not settings.enabled:
        return None
    if source_type == "call" and (
        not settings.after_calls or (call_duration_seconds or 0) < settings.min_call_seconds
    ):
        return None
    if source_type == "ticket" and not settings.after_tickets:
        return None
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != tenant_id or not customer.phone:
        return None

    already = (
        await db.execute(
            select(CsatSurvey.id).where(
                CsatSurvey.tenant_id == tenant_id,
                CsatSurvey.source_type == source_type,
                CsatSurvey.source_id == source_id,
            )
        )
    ).first()
    if already is not None:
        return None
    now = datetime.now(UTC)
    recent = (
        await db.execute(
            select(CsatSurvey.id).where(
                CsatSurvey.tenant_id == tenant_id,
                CsatSurvey.customer_id == customer_id,
                CsatSurvey.status.in_(("pending", "sent", "answered")),
                CsatSurvey.created_at >= now - timedelta(hours=settings.cooldown_hours),
            )
        )
    ).first()
    if recent is not None:
        return None

    survey = CsatSurvey(
        tenant_id=tenant_id,
        customer_id=customer_id,
        source_type=source_type,
        source_id=source_id,
        status="pending",
        send_at=now + timedelta(minutes=settings.delay_minutes),
    )
    db.add(survey)
    await db.flush()
    return survey


async def schedule_after_call(db: AsyncSession, call) -> None:
    """Best effort: a survey problem must never affect finishing a call."""
    try:
        if await schedule_survey(
            db,
            tenant_id=call.tenant_id,
            customer_id=call.customer_id,
            source_type="call",
            source_id=call.id,
            call_duration_seconds=call.duration_seconds,
        ):
            await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("could not schedule CSAT survey for call %s", call.id, exc_info=True)


async def schedule_after_ticket(db: AsyncSession, ticket: Ticket) -> None:
    try:
        if await schedule_survey(
            db,
            tenant_id=ticket.tenant_id,
            customer_id=ticket.customer_id,
            source_type="ticket",
            source_id=ticket.id,
        ):
            await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("could not schedule CSAT survey for ticket %s", ticket.id, exc_info=True)


# ---------------------------------------------------------------- sending


async def _whatsapp_window_open(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> bool:
    from app.services.whatsapp_templates import window_open

    from packages.database.models import Conversation

    last = (
        await db.execute(
            select(func.max(Message.created_at))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.customer_id == customer_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
                Message.direction == MessageDirection.INBOUND,
            )
        )
    ).scalar_one()
    return window_open(last)


async def _send_whatsapp(db: AsyncSession, tenant_id: uuid.UUID, customer: Customer, body: str) -> None:
    from app.integrations.whatsapp import WhatsAppAPIError, send_text_message

    account = (
        await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    ).scalars().first()
    if account is None:
        raise SurveySendError("No WhatsApp number is connected")
    from app.services import plans

    try:
        await plans.ensure_whatsapp_available(db, tenant_id)
        await send_text_message(account, to=customer.phone.lstrip("+"), body=body)
    except plans.PlanLimitError as exc:
        raise SurveySendError(exc.message) from exc
    except WhatsAppAPIError as exc:
        raise SurveySendError(f"WhatsApp: {exc}") from exc


async def _send_sms(db: AsyncSession, tenant_id: uuid.UUID, customer: Customer, body: str) -> None:
    from app.integrations.channel_senders import ChannelSendError, send_sms_message
    from app.services.channels import get_channel_settings

    from packages.database.models import PhoneNumber

    sms = await get_channel_settings(db, tenant_id, "sms")
    # Replies only reach us when the SMS channel is enabled, so require it.
    if sms is None or not sms.enabled:
        raise SurveySendError("The SMS channel is not enabled")
    from_ = (sms.config or {}).get("from_number")
    if not from_:
        number = (
            await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id))
        ).scalars().first()
        from_ = number.e164_number if number else None
    if not from_:
        raise SurveySendError("No sending phone number is configured")
    try:
        await send_sms_message(db, tenant_id, to=customer.phone, from_=from_, body=body)
    except ChannelSendError as exc:
        raise SurveySendError(str(exc)) from exc


async def _deliver(db: AsyncSession, survey: CsatSurvey, settings: CsatSettings) -> str:
    customer = await db.get(Customer, survey.customer_id)
    if customer is None or not customer.phone:
        raise SurveySendError("The customer has no phone number")
    channel = settings.channel if settings.channel in CHANNELS else "auto"
    if channel == "auto":
        channel = (
            "whatsapp"
            if await _whatsapp_window_open(db, survey.tenant_id, survey.customer_id)
            else "sms"
        )
    if channel == "whatsapp":
        if not await _whatsapp_window_open(db, survey.tenant_id, survey.customer_id):
            raise SurveySendError("WhatsApp's 24-hour window is closed for this customer")
        await _send_whatsapp(db, survey.tenant_id, customer, settings.question)
    else:
        await _send_sms(db, survey.tenant_id, customer, settings.question)
    return channel


async def send_due_surveys(db: AsyncSession, *, now: datetime | None = None) -> dict:
    """Sends pending surveys that are due and expires unanswered ones."""
    now = now or datetime.now(UTC)
    sent = failed = 0

    due = (
        await db.execute(
            select(CsatSurvey)
            .where(CsatSurvey.status == "pending", CsatSurvey.send_at <= now)
            .order_by(CsatSurvey.send_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for survey in due:
        settings = await get_settings_row(db, survey.tenant_id)
        if not settings.enabled:
            survey.status, survey.error = "failed", "Surveys were switched off before sending"
            failed += 1
            continue
        try:
            survey.channel = await _deliver(db, survey, settings)
        except SurveySendError as exc:
            survey.status, survey.error = "failed", str(exc)[:500]
            failed += 1
        except Exception as exc:  # provider/network problem: don't lose the batch
            logger.warning("CSAT send failed for %s", survey.id, exc_info=True)
            survey.status, survey.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            failed += 1
        else:
            survey.status = "sent"
            survey.sent_at = now
            survey.expires_at = now + timedelta(hours=settings.expire_hours)
            sent += 1
        await db.flush()

    expired = (
        await db.execute(
            select(CsatSurvey).where(CsatSurvey.status == "sent", CsatSurvey.expires_at <= now)
        )
    ).scalars().all()
    for survey in expired:
        survey.status = "expired"
    await db.commit()
    return {"sent": sent, "failed": failed, "expired": len(expired)}


# ---------------------------------------------------------------- replies


async def capture_reply(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    channel: ConversationChannel,
    text: str,
) -> str | None:
    """If this inbound WhatsApp/SMS text answers the customer's open survey,
    record it and return the thank-you to send. Otherwise None, and the
    message continues to the AI agent as usual."""
    if customer_id is None or channel not in (ConversationChannel.WHATSAPP, ConversationChannel.SMS):
        return None
    parsed = parse_rating(text)
    if parsed is None:
        return None
    now = datetime.now(UTC)
    survey = (
        await db.execute(
            select(CsatSurvey)
            .where(
                CsatSurvey.tenant_id == tenant_id,
                CsatSurvey.customer_id == customer_id,
                CsatSurvey.status == "sent",
                CsatSurvey.channel == channel.value,
                CsatSurvey.expires_at > now,
            )
            .order_by(CsatSurvey.sent_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if survey is None:
        return None

    rating, comment = parsed
    survey.status, survey.rating, survey.comment, survey.answered_at = "answered", rating, comment, now
    settings = await get_settings_row(db, tenant_id)
    if settings.low_score_ticket_threshold and rating <= settings.low_score_ticket_threshold:
        await _open_low_score_ticket(db, survey, rating, comment)
    await db.flush()
    enqueue_webhook_event(
        tenant_id=tenant_id,
        event="csat.received",
        data={
            "survey_id": str(survey.id),
            "customer_id": str(customer_id),
            "source_type": survey.source_type,
            "source_id": str(survey.source_id),
            "channel": survey.channel,
            "rating": rating,
            "comment": comment,
        },
    )
    return settings.thank_you


async def _open_low_score_ticket(
    db: AsyncSession, survey: CsatSurvey, rating: int, comment: str | None
) -> None:
    from app.services.operations import apply_sla_safely

    subject = f"Low CSAT ({rating}/5) after {survey.source_type}"
    if comment:
        subject = f"{subject}: {comment}"
    ticket = Ticket(
        tenant_id=survey.tenant_id,
        customer_id=survey.customer_id,
        subject=subject[:255],
        priority=TicketPriority.HIGH,
    )
    db.add(ticket)
    await db.flush()
    await apply_sla_safely(db, survey.tenant_id, ticket)


# ---------------------------------------------------------------- reporting


async def summary(db: AsyncSession, tenant_id: uuid.UUID, *, days: int = 30) -> dict:
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 365)))
    base = (CsatSurvey.tenant_id == tenant_id, CsatSurvey.created_at >= since)

    status_rows = await db.execute(
        select(CsatSurvey.status, func.count()).where(*base).group_by(CsatSurvey.status)
    )
    by_status = {k: n for k, n in status_rows}
    rating_rows = await db.execute(
        select(CsatSurvey.rating, func.count())
        .where(*base, CsatSurvey.status == "answered")
        .group_by(CsatSurvey.rating)
    )
    distribution = {str(i): 0 for i in range(1, 6)}
    for rating, n in rating_rows:
        distribution[str(rating)] = n
    answered = sum(distribution.values())
    average = (
        round(sum(int(r) * n for r, n in distribution.items()) / answered, 2) if answered else None
    )
    source_rows = await db.execute(
        select(CsatSurvey.source_type, func.avg(CsatSurvey.rating), func.count())
        .where(*base, CsatSurvey.status == "answered")
        .group_by(CsatSurvey.source_type)
    )
    delivered = by_status.get("sent", 0) + by_status.get("answered", 0) + by_status.get("expired", 0)
    return {
        "days": days,
        "surveys_sent": delivered,
        "responses": answered,
        "response_rate": round(answered / delivered, 3) if delivered else None,
        "average_rating": average,
        # share of responses that were 4 or 5, the usual "CSAT %" figure
        "csat_percent": round(100 * (distribution["4"] + distribution["5"]) / answered, 1)
        if answered
        else None,
        "distribution": distribution,
        "by_source": {
            s: {"average_rating": round(float(avg), 2), "responses": n} for s, avg, n in source_rows
        },
        "by_status": by_status,
    }
