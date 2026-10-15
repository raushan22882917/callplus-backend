"""Campaign execution — a sequential broadcast throttled by a fixed delay
between sends (campaign_*_interval_seconds) to stay under Twilio/Meta rate
limits. Transient provider failures are retried with backoff
(campaign_max_send_attempts); config errors fail the contact immediately."""

import asyncio
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.integrations.twilio_client import TwilioClientError, place_outbound_call
from app.integrations.whatsapp import WhatsAppAPIError, send_template_message
from app.services import dialer, telephony_plus
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
    Customer,
    PhoneNumber,
    WhatsAppAccount,
)
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
        else:
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
        interval = (
            settings.campaign_voice_interval_seconds
            if campaign.channel == CampaignChannel.VOICE
            else settings.campaign_whatsapp_interval_seconds
        )

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
            if customer is None or not customer.phone:
                contact.status = CampaignContactStatus.FAILED
                contact.result = {"error": "customer has no phone number"}
                contact.attempted_at = datetime.now(UTC)
                continue

            attempts = 0
            while True:
                attempts += 1
                try:
                    if campaign.channel == CampaignChannel.VOICE:
                        await _place_campaign_call(db, campaign, phone_number, customer)
                    else:
                        await _send_campaign_whatsapp(db, campaign, whatsapp_account, customer)
                    contact.status = CampaignContactStatus.SENT
                    contact.result = {"attempts": attempts} if attempts > 1 else contact.result
                    break
                except ValueError as exc:  # misconfiguration — retrying can't help
                    contact.status = CampaignContactStatus.FAILED
                    contact.result = {"error": str(exc), "attempts": attempts}
                    break
                except (TwilioClientError, TwilioException, WhatsAppAPIError) as exc:
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


async def _place_campaign_call(db, campaign: Campaign, phone_number, customer: Customer) -> Call:
    if phone_number is None:
        raise ValueError("no phone number assigned to this campaign's AI agent")

    call = Call(
        tenant_id=campaign.tenant_id,
        customer_id=customer.id,
        ai_agent_id=campaign.ai_agent_id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        from_number=phone_number.e164_number,
        to_number=customer.phone,
    )
    db.add(call)
    await db.flush()

    settings = get_settings()
    voice_webhook_url = (
        f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/voice?call_id={call.id}"
    )
    status_callback_url = f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/status"
    try:
        call_sid = place_outbound_call(
            to=customer.phone,
            from_=phone_number.e164_number,
            voice_webhook_url=voice_webhook_url,
            status_callback_url=status_callback_url,
            amd_callback_url=await amd_callback_for_agent(db, campaign.ai_agent_id),
        )
    except (TwilioClientError, TwilioException):
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
        )
        if not decision.allowed:
            if decision.reason != "outside_call_window":  # blocked / DND: never dial
                contact.status = CampaignContactStatus.FAILED
                contact.result = {"skipped": decision.reason}
                contact.attempted_at = now
            continue  # outside the window: stays pending for a later pass

        state = await telephony_plus.get_or_create_state(db, campaign, contact)
        state.attempts += 1
        contact.attempted_at = now
        try:
            call = await _place_campaign_call(db, campaign, phone_number, customer)
        except ValueError as exc:  # misconfiguration — retrying can't help
            contact.status = CampaignContactStatus.FAILED
            contact.result = {"error": str(exc), "attempts": state.attempts}
            continue
        except (TwilioClientError, TwilioException):
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


async def _send_campaign_whatsapp(db, campaign: Campaign, whatsapp_account, customer: Customer) -> None:
    if whatsapp_account is None:
        raise ValueError("no WhatsApp account connected for this tenant")
    if not campaign.whatsapp_template_name:
        raise ValueError("campaign has no whatsapp_template_name")
    from app.services import whatsapp_templates as tpl

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
