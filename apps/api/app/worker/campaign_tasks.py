"""Campaign execution — a sequential broadcast throttled by a fixed delay
between sends (campaign_*_interval_seconds) to stay under Twilio/Meta/email
rate limits. Transient provider failures are retried with backoff
(campaign_max_send_attempts); config errors fail the contact immediately."""

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.integrations.channel_senders import ChannelSendError, new_message_id
from app.integrations.credentials import get_email_sender
from app.integrations.credentials import SharedAccountError, require_own_twilio
from app.integrations.email_client import send_email
from app.integrations.twilio_client import TwilioClientError, place_outbound_call
from app.integrations.whatsapp import WhatsAppAPIError, send_template_message
from app.services import dialer, email_compliance, exotel, plans, privacy, telephony_plus
from app.services.cost_controls import SpendGuardError, guard_outbound
from app.services.voicemail_detection import amd_callback_for_agent
from app.services.webhooks import enqueue_webhook_event
from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine
from sqlalchemy import func, or_, select
from twilio.base.exceptions import TwilioException

from packages.database.models import (
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
    ConversationChannel,
    Customer,
    DndEntry,
    MessageDirection,
    MessageSender,
    PhoneNumber,
    Tenant,
    WhatsAppAccount,
)

logger = logging.getLogger(__name__)
from packages.database.models.telephony_plus import CampaignContactState
from packages.database.session import session_scope


async def _run_campaign(campaign_id: str) -> None:
    ensure_engine()
    async with session_scope() as db:
        campaign = await db.get(Campaign, uuid.UUID(campaign_id))
        if campaign is None:
            return

        contacts_result = await db.execute(
            select(CampaignContact).where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.status == CampaignContactStatus.PENDING,
            )
        )
        contacts = contacts_result.scalars().all()

        phone_number = None
        whatsapp_account = None
        if campaign.channel == CampaignChannel.VOICE:
            result = await db.execute(
                select(PhoneNumber).where(
                    PhoneNumber.tenant_id == campaign.tenant_id,
                    PhoneNumber.ai_agent_id == campaign.ai_agent_id,
                )
            )
            phone_number = result.scalars().first()
        elif campaign.channel == CampaignChannel.WHATSAPP:
            result = await db.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == campaign.tenant_id)
            )
            whatsapp_account = result.scalars().first()

        if campaign.channel == CampaignChannel.VOICE:
            dialer_settings = await telephony_plus.get_dialer_settings(db, campaign.id)
            if dialer_settings is not None:
                # Campaigns with dialer settings are paced/windowed by the
                # dialer (see _run_dialer_pass); others keep the sequential path.
                await _run_dialer_pass(db, campaign, dialer_settings, phone_number)
                return

        settings = get_settings()
        interval = _campaign_interval(settings, campaign.channel)
        requires_consent = False
        if campaign.channel == CampaignChannel.EMAIL:
            requires_consent = (await email_compliance.profile(db, campaign.tenant_id)).email_marketing_requires_consent

        for index, contact in enumerate(contacts):
            if index > 0 and interval > 0:
                await asyncio.sleep(interval)
            # Re-check status before each send so a pause (POST
            # .../pause) takes effect between contacts rather than only
            # once the whole batch has been dispatched.
            await db.refresh(campaign)
            if campaign.status != CampaignStatus.RUNNING:
                # Persist whatever contacts were already sent/flushed this
                # run before stopping — session_scope does not auto-commit.
                await db.commit()
                return

            customer = await db.get(Customer, contact.customer_id)
            missing = _missing_destination(campaign, customer)
            if missing:
                contact.status = CampaignContactStatus.FAILED
                contact.result = {"error": missing}
                contact.attempted_at = datetime.now(UTC)
                continue
            if campaign.channel == CampaignChannel.EMAIL:
                # Checked per contact, so an unsubscribe that arrives mid-run still stops the next send.
                blocked = await email_compliance.blocked_reason(
                    db, campaign.tenant_id, customer.email, requires_consent=requires_consent
                )
                if blocked:
                    contact.status = CampaignContactStatus.FAILED
                    contact.result = {"error": blocked, "skipped": True}
                    contact.attempted_at = datetime.now(UTC)
                    continue

            if campaign.channel in (CampaignChannel.WHATSAPP, CampaignChannel.SMS):
                # A number that opted out, on any channel, is on the do-not-disturb list.
                numbers = {customer.phone, privacy._clean_phone(customer.phone)} - {None}
                on_dnd = (
                    await db.execute(
                        select(DndEntry.id).where(DndEntry.tenant_id == campaign.tenant_id, DndEntry.e164_number.in_(numbers))
                    )
                ).first()
                if on_dnd:
                    contact.status = CampaignContactStatus.FAILED
                    contact.result = {"error": "number is on the do-not-disturb list", "skipped": True}
                    contact.attempted_at = datetime.now(UTC)
                    continue

            attempts = 0
            while True:
                attempts += 1
                try:
                    sent = None
                    if campaign.channel == CampaignChannel.VOICE:
                        await _place_campaign_call(db, campaign, phone_number, customer)
                    elif campaign.channel == CampaignChannel.WHATSAPP:
                        await _send_campaign_whatsapp(db, campaign, whatsapp_account, customer)
                    elif campaign.channel == CampaignChannel.SMS:
                        sent = await _send_campaign_sms(db, campaign, customer)
                    else:
                        sent = await _send_campaign_email(db, campaign, customer)
                    contact.status = CampaignContactStatus.SENT
                    if sent:
                        contact.result = {**sent, **({"attempts": attempts} if attempts > 1 else {})}
                    elif attempts > 1:
                        contact.result = {"attempts": attempts}
                    break
                except SpendGuardError:
                    # Over budget or a cap: stop the campaign (contacts stay pending
                    # so it can be resumed) rather than failing them one by one.
                    campaign.status = CampaignStatus.PAUSED
                    await db.commit()
                    return
                except ValueError as exc:  # misconfiguration — retrying can't help
                    contact.status = CampaignContactStatus.FAILED
                    contact.result = {"error": str(exc), "attempts": attempts}
                    break
                except (
                    TwilioClientError,
                    TwilioException,
                    WhatsAppAPIError,
                    exotel.ExotelError,
                    ChannelSendError,
                ) as exc:
                    if attempts >= settings.campaign_max_send_attempts:
                        contact.status = CampaignContactStatus.FAILED
                        contact.result = {"error": str(exc), "attempts": attempts}
                        break
                    await asyncio.sleep(settings.campaign_retry_backoff_seconds * attempts)
            contact.attempted_at = datetime.now(UTC)
            await db.flush()

        campaign.status = CampaignStatus.COMPLETED
        await db.commit()
        enqueue_webhook_event(
            tenant_id=campaign.tenant_id,
            event="campaign.completed",
            data={"campaign_id": str(campaign.id), "name": campaign.name},
        )


def campaign_mission(campaign: Campaign) -> dict | None:
    if not (campaign.goal or campaign.questions):
        return None
    return {
        "campaign_id": str(campaign.id),
        "goal": campaign.goal or "",
        "questions": list(campaign.questions or []),
    }


async def _place_campaign_call(db, campaign: Campaign, phone_number, customer: Customer) -> Call:
    if phone_number is None:
        raise ValueError("no phone number assigned to this campaign's AI agent")
    chosen = await exotel.prefer_indian_cli(
        db, campaign.tenant_id, customer.phone, phone_number.e164_number
    )
    if chosen and chosen != phone_number.e164_number:
        swapped = (
            await db.execute(
                select(PhoneNumber).where(
                    PhoneNumber.tenant_id == campaign.tenant_id, PhoneNumber.e164_number == chosen
                )
            )
        ).scalar_one_or_none()
        if swapped is not None:
            phone_number = swapped
    await guard_outbound(db, campaign.tenant_id)

    call = Call(
        tenant_id=campaign.tenant_id,
        customer_id=customer.id,
        ai_agent_id=campaign.ai_agent_id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        from_number=phone_number.e164_number,
        to_number=customer.phone,
        mission=campaign_mission(campaign),
    )
    db.add(call)
    await db.flush()

    settings = get_settings()
    voice_webhook_url = (
        f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/voice?call_id={call.id}"
    )
    status_callback_url = f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/status"
    try:
        if phone_number.provider == exotel.PROVIDER:
            call_sid = await exotel.place_outbound_call(db, call)
        else:
            await require_own_twilio(db, campaign.tenant_id)
            call_sid = place_outbound_call(
                to=customer.phone,
                from_=phone_number.e164_number,
                voice_webhook_url=voice_webhook_url,
                status_callback_url=status_callback_url,
                amd_callback_url=await amd_callback_for_agent(db, campaign.ai_agent_id),
            )
    except (TwilioClientError, TwilioException, exotel.ExotelError, SharedAccountError):
        # Don't leave a phantom QUEUED row behind; a retry makes a fresh one.
        call.status = CallStatus.FAILED
        raise
    call.provider_call_sid = call_sid
    call.status = CallStatus.RINGING
    return call


DIALER_POLL_SECONDS = 15
DIALER_PREVIEW_POLL_SECONDS = 60


async def _run_dialer_pass(db, campaign: Campaign, ds, phone_number) -> None:
    """One paced pass: reconcile finished calls into dispositions, ask
    dialer.next_batch how many calls to place, place them (DND/blocklist/call
    windows enforced per contact in its own timezone), then re-enqueue
    ourselves until nothing is pending or in flight."""
    await db.refresh(campaign)
    if campaign.status != CampaignStatus.RUNNING:
        return

    now = datetime.now(UTC)
    await telephony_plus.reconcile_finished_calls(db, campaign, ds)
    snapshot = await telephony_plus.dialer_snapshot(db, campaign, ds, now)
    candidates = await telephony_plus.eligible_contacts(db, campaign, now)

    batch = dialer.next_batch(
        mode=ds.mode,
        available_agents=max(ds.max_concurrent - snapshot["in_progress"], 0),
        in_flight=snapshot["ringing"],
        answer_rate=snapshot["answer_rate"],
        pacing_ratio=ds.pacing_ratio,
        max_concurrent=max(ds.max_concurrent - snapshot["in_progress"], 0),
        abandon_rate=snapshot["abandon_rate"],
        abandon_rate_cap=ds.abandon_rate_cap,
        remaining_contacts=len(candidates),
        daily_remaining=snapshot["daily_remaining"],
    )

    settings = get_settings()
    placed = 0
    for contact, state in candidates:
        if placed >= batch:
            break
        customer = await db.get(Customer, contact.customer_id)
        if customer is None or not customer.phone:
            contact.status = CampaignContactStatus.FAILED
            contact.result = {"error": "customer has no phone number"}
            contact.attempted_at = now
            continue

        zone = dialer.timezone_for_contact(
            customer.phone, extra=customer.extra, default=ds.default_timezone
        )
        decision = await telephony_plus.check_outbound_allowed(
            db,
            campaign.tenant_id,
            customer.phone,
            now=now,
            tz_name=zone,
            window=(ds.call_window_start, ds.call_window_end),
            quiet_hours=(ds.quiet_hours_start, ds.quiet_hours_end),
            max_per_day=ds.per_customer_daily_cap,
            max_per_week=ds.per_customer_weekly_cap,
        )
        if not decision.allowed:
            if decision.reason not in telephony_plus.DEFER_REASONS:  # blocked / DND: never dial
                contact.status = CampaignContactStatus.FAILED
                contact.result = {"skipped": decision.reason}
                contact.attempted_at = now
            continue  # outside the window: stays pending for a later pass
        if not await privacy.has_outbound_consent(db, campaign.tenant_id, customer.phone):
            contact.status = CampaignContactStatus.FAILED
            contact.result = {"skipped": "no_consent"}
            contact.attempted_at = now
            continue

        state = await telephony_plus.get_or_create_state(db, campaign, contact)
        state.attempts += 1
        previous_attempt = contact.attempted_at
        contact.attempted_at = now
        try:
            call = await _place_campaign_call(db, campaign, phone_number, customer)
        except SpendGuardError as exc:
            # Not this contact's fault: undo its attempt and stop placing for now.
            state.attempts -= 1
            contact.attempted_at = previous_attempt
            if exc.code == "budget_exhausted":
                campaign.status = CampaignStatus.PAUSED
            break  # daily/concurrency caps clear by themselves; the next pass retries
        except ValueError as exc:  # misconfiguration — retrying can't help
            contact.status = CampaignContactStatus.FAILED
            contact.result = {"error": str(exc), "attempts": state.attempts}
            continue
        except (TwilioClientError, TwilioException, exotel.ExotelError, SharedAccountError):
            await telephony_plus.record_disposition(
                db, campaign, contact, disposition="failed", settings_row=ds, source="auto"
            )
            continue
        state.last_call_id = call.id
        state.next_attempt_at = None
        contact.status = CampaignContactStatus.SENT
        contact.result = {**(contact.result or {}), "call_id": str(call.id)}
        placed += 1
        await db.flush()
        if settings.campaign_voice_interval_seconds > 0 and placed < batch:
            await asyncio.sleep(settings.campaign_voice_interval_seconds)

    await db.flush()
    pending = (
        await db.execute(
            select(func.count())
            .select_from(CampaignContact)
            .where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.status == CampaignContactStatus.PENDING,
            )
        )
    ).scalar_one()
    awaiting = (
        await db.execute(
            select(func.count())
            .select_from(CampaignContactState)
            .where(
                CampaignContactState.campaign_id == campaign.id,
                CampaignContactState.last_call_id.is_not(None),
                or_(
                    CampaignContactState.last_processed_call_id.is_(None),
                    CampaignContactState.last_call_id != CampaignContactState.last_processed_call_id,
                ),
            )
        )
    ).scalar_one()

    if pending == 0 and awaiting == 0:
        campaign.status = CampaignStatus.COMPLETED
        await db.commit()
        enqueue_webhook_event(
            tenant_id=campaign.tenant_id,
            event="campaign.completed",
            data={"campaign_id": str(campaign.id), "name": campaign.name},
        )
        return

    await db.commit()
    run_campaign_task.apply_async(
        args=[str(campaign.id)],
        countdown=DIALER_PREVIEW_POLL_SECONDS if ds.mode == "preview" else DIALER_POLL_SECONDS,
    )


_EMAIL_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _campaign_interval(settings, channel: CampaignChannel) -> float:
    if channel == CampaignChannel.VOICE:
        return settings.campaign_voice_interval_seconds
    if channel == CampaignChannel.EMAIL:
        return settings.campaign_email_interval_seconds
    if channel == CampaignChannel.SMS:
        return settings.campaign_sms_interval_seconds
    return settings.campaign_whatsapp_interval_seconds


def _missing_destination(campaign: Campaign, customer: Customer | None) -> str | None:
    if customer is None:
        return "customer not found"
    if campaign.channel == CampaignChannel.EMAIL:
        return None if customer.email else "customer has no email address"
    return None if customer.phone else "customer has no phone number"


def render_campaign_email(template: str, customer: Customer) -> str:
    """Fill {{name}}, {{first_name}}, {{email}} and {{phone}}. Unknown tokens are removed."""
    name = (customer.name or "").strip()
    values = {
        "name": name,
        "first_name": name.split()[0] if name else "",
        "email": customer.email or "",
        "phone": customer.phone or "",
    }

    def replace(match: re.Match) -> str:
        return values.get(match.group(1).lower(), "")

    return _EMAIL_PLACEHOLDER.sub(replace, template or "")


def _email_html(body: str) -> str:
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


async def _send_campaign_email(db, campaign: Campaign, customer: Customer) -> dict:
    if not (campaign.email_subject or "").strip() or not (campaign.email_body or "").strip():
        raise ValueError("campaign has no email subject or body")
    from app.services.mailbox import MailboxError, connected_mailbox, deliver

    settings = get_settings()
    box = await connected_mailbox(db, campaign.tenant_id)
    own_sender = await get_email_sender(db, campaign.tenant_id) if box is None else None
    if box is None and own_sender is None and not settings.email_api_key and not settings.smtp_host:
        raise ValueError("Connect a mailbox (Gmail, Microsoft, or SMTP) or an email sending key (Resend or Brevo)")
    sender = await email_compliance.sender_identity(db, campaign.tenant_id)
    if sender is None:
        raise ValueError("Add your business postal address in Settings > Privacy before sending marketing email")
    subject = render_campaign_email(campaign.email_subject or "", customer)
    body = render_campaign_email(campaign.email_body or "", customer)
    unsubscribe_link = email_compliance.unsubscribe_url(campaign.tenant_id, customer.email or "")
    unsubscribe_headers = email_compliance.unsubscribe_headers(unsubscribe_link)
    text_body = body + email_compliance.footer_text(sender, unsubscribe_link)
    html_body = _email_html(body) + email_compliance.footer_html(sender, unsubscribe_link)
    # The reservation counts against the plan. A refused allowance pauses the
    # campaign (SpendGuardError). A provider failure deletes the reservation
    # so a retry is not charged twice.
    event = await plans.reserve_email(db, campaign.tenant_id)
    message_id = new_message_id()
    headers = {"Message-ID": message_id, **unsubscribe_headers}
    if campaign.email_reply_to:
        headers["Reply-To"] = campaign.email_reply_to
    try:
        if box is not None:
            message_id = await deliver(
                db,
                box,
                to_email=customer.email or "",
                subject=subject,
                text=text_body,
                html=html_body,
                reply_to=campaign.email_reply_to,
                message_id=message_id,
                extra_headers=unsubscribe_headers,
            )
        else:
            await send_email(
                to_email=customer.email or "",
                subject=subject,
                html_body=html_body,
                headers=headers,
                tenant_id=str(campaign.tenant_id),
                credentials=own_sender,
            )
    except (MailboxError, Exception) as exc:
        await db.delete(event)
        await db.flush()
        if isinstance(exc, MailboxError):
            raise ChannelSendError(str(exc)) from exc
        raise ChannelSendError(f"Email not sent: {exc}") from exc
    await plans.sync_overage_invoice(db, campaign.tenant_id)
    await _record_campaign_email(db, campaign, customer, subject, body, message_id)
    return {"to": customer.email, "subject": subject, "message_id": message_id}


async def _record_campaign_email(db, campaign: Campaign, customer: Customer, subject: str, body: str, message_id: str) -> None:
    """Opens an inbox thread so a reply can be matched on this Message-ID."""
    from app.services import channels as channel_svc

    try:
        conversation, _, _ = await channel_svc.ensure_thread(
            db,
            campaign.tenant_id,
            ConversationChannel.EMAIL,
            message_id[:255],
            customer_id=customer.id,
            contact_address=customer.email,
            subject=subject,
        )
        await channel_svc.record_message(
            db,
            campaign.tenant_id,
            conversation.id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.AI_AGENT,
            text=body,
            provider_message_id=message_id[:128],
        )
    except Exception:
        logger.exception("campaign email sent but the inbox thread was not saved")


async def _send_campaign_sms(db, campaign: Campaign, customer: Customer) -> dict:
    """One SMS from the workspace's own number, filed in the customer's SMS thread so a reply (or STOP) lands there."""
    from app.integrations.channel_senders import send_sms_message
    from app.services import channels as channel_svc
    from app.services import follow_up as follow_up_svc
    from app.services.outreach import SMS_FOOTER

    if not (campaign.sms_body or "").strip():
        raise ValueError("campaign has no SMS message")
    from_, why = await follow_up_svc._sms_from(db, campaign.tenant_id)
    if from_ is None:
        raise ValueError(why or "SMS is not ready")
    body = render_campaign_email(campaign.sms_body or "", customer).strip() + SMS_FOOTER
    provider_id = await send_sms_message(db, campaign.tenant_id, to=customer.phone, from_=from_, body=body)
    try:
        conversation, _, _ = await channel_svc.ensure_thread(
            db, campaign.tenant_id, ConversationChannel.SMS, f"{customer.phone}|{from_}",
            customer_id=customer.id, contact_address=customer.phone, via_address=from_,
        )
        await channel_svc.record_message(
            db, campaign.tenant_id, conversation.id, direction=MessageDirection.OUTBOUND,
            sender=MessageSender.AI_AGENT, text=body, provider_message_id=provider_id,
        )
    except Exception:
        logger.exception("campaign SMS sent but the inbox thread was not saved")
    return {"to": customer.phone, "message_id": provider_id}


async def _send_campaign_whatsapp(db, campaign: Campaign, whatsapp_account, customer: Customer) -> None:
    if whatsapp_account is None:
        raise ValueError("no WhatsApp account connected for this tenant")
    if not campaign.whatsapp_template_name:
        raise ValueError("campaign has no whatsapp_template_name")
    from app.services import whatsapp_templates as tpl

    # The send claims the allowance and raises PlanLimitError (a SpendGuardError),
    # which pauses the campaign with its remaining contacts still pending.
    template = await tpl.find_approved(db, campaign.tenant_id, name=campaign.whatsapp_template_name)
    if template is not None:  # the business's own template: fill {{name}} etc. per customer
        values = await tpl.context_values(db, campaign.tenant_id, customer)
        await tpl.send_template(
            db, campaign.tenant_id, to=customer.phone, template=template, values=values, account=whatsapp_account
        )
        return
    await send_template_message(
        whatsapp_account, to=customer.phone, template_name=campaign.whatsapp_template_name
    )


@celery_app.task(name="campaigns.run")
def run_campaign_task(campaign_id: str) -> None:
    asyncio.run(_run_campaign(campaign_id))


async def _start_due_campaigns() -> int:
    """Starts every campaign whose scheduled time has come (status SCHEDULED, scheduled_at <= now)."""
    ensure_engine()
    started: list[str] = []
    async with session_scope() as db:
        result = await db.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.SCHEDULED,
                Campaign.scheduled_at <= datetime.now(UTC),
            )
        )
        for campaign in result.scalars().all():
            if campaign.channel == CampaignChannel.VOICE:
                tenant = await db.get(Tenant, campaign.tenant_id)
                if tenant is None or tenant.a2p_declared_at is None:
                    continue
            campaign.status = CampaignStatus.RUNNING
            started.append(str(campaign.id))
        await db.commit()
    for campaign_id in started:
        run_campaign_task.delay(campaign_id)
    return len(started)


@celery_app.task(name="campaigns.start_due")
def start_due_campaigns_task() -> int:
    return asyncio.run(_start_due_campaigns())
