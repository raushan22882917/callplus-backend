"""DB-backed orchestration for telephony_plus: inbound routing (forwarding /
IVR / number routing), business-hours lookup (defensive), outbound DND guard,
campaign dispositions, CRM-lead/ticket routing and campaign statistics.
Pure logic lives in app/services/ivr.py and app/services/dialer.py."""

import importlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import TwilioClientError, place_outbound_call
from app.services import dialer, ivr
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import (
    AIAgent,
    BlockedCaller,
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    CampaignContact,
    CampaignContactStatus,
    Customer,
    Ticket,
    TicketPriority,
)
from packages.database.models.telephony_plus import (
    CallForwardingRule,
    CallQueue,
    CampaignContactState,
    CampaignDialerSettings,
    CampaignOutcome,
    DndEntry,
    IvrMenu,
    NumberRouting,
    RingGroup,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/telephony_plus"

CONNECTED = {"answered", "interested", "qualified", "not_interested", "callback", "wrong_number", "dnc"}
RETRYABLE = {"no_answer", "busy", "failed", "voicemail", "abandoned"}
DISPOSITIONS = sorted(CONNECTED | RETRYABLE)


def base_url() -> str:
    return f"{get_settings().public_base_url}{API_PREFIX}"


async def table_exists(db: AsyncSession, name: str) -> bool:
    conn = await db.connection()
    return await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(name))


# --- business hours (defensive: tables belong to another module) -------------


async def is_open_now(db: AsyncSession, tenant_id: uuid.UUID, now: datetime | None = None) -> bool:
    """True when the tenant is open, or when no business-hours data exists at
    runtime (module absent / table missing / no row configured)."""
    now = now or datetime.now(UTC)
    try:
        workspace = importlib.import_module("packages.database.models.workspace")
        hours_model = workspace.BusinessHours
        holiday_model = workspace.Holiday
    except (ImportError, AttributeError):
        return True
    if not (await table_exists(db, "business_hours") and await table_exists(db, "holidays")):
        return True

    hours = (
        await db.execute(select(hours_model).where(hours_model.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if hours is None:
        return True
    zone = dialer.safe_zone(hours.timezone)
    today = now.astimezone(zone).date()
    holiday = (
        await db.execute(
            select(holiday_model.id).where(
                holiday_model.tenant_id == tenant_id, holiday_model.day == today
            )
        )
    ).first()
    return ivr.is_open_now(
        now,
        tz_name=hours.timezone,
        windows=list(hours.windows or []),
        holiday_days={today} if holiday else None,
    )


# --- outbound guard ------------------------------------------------------------


@dataclass(frozen=True)
class DialDecision:
    allowed: bool
    reason: str | None = None


async def check_outbound_allowed(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    phone: str,
    *,
    now: datetime | None = None,
    quiet_hours: tuple[str, str] | None = None,
    window: tuple[str, str] | None = None,
    tz_name: str | None = None,
) -> DialDecision:
    """Blocklist + dnd_registry always; quiet hours / call window when the
    caller passes them (campaigns do; one-off manual calls only get DND)."""
    blocked = (
        await db.execute(
            select(BlockedCaller.id).where(
                BlockedCaller.tenant_id == tenant_id, BlockedCaller.e164_number == phone
            )
        )
    ).first()
    if blocked:
        return DialDecision(False, "blocked")

    if await table_exists(db, "dnd_registry"):
        dnd = (
            await db.execute(
                select(DndEntry.id).where(
                    DndEntry.tenant_id == tenant_id, DndEntry.e164_number == phone
                )
            )
        ).first()
        if dnd:
            return DialDecision(False, "dnd")

    if quiet_hours or window:
        now = now or datetime.now(UTC)
        zone = tz_name or "Asia/Kolkata"
        start, end = window or ("00:00", "00:00")
        q_start, q_end = quiet_hours or (None, None)
        if not dialer.can_call_now(
            now, tz_name=zone, window_start=start, window_end=end, quiet_start=q_start, quiet_end=q_end
        ):
            return DialDecision(False, "outside_call_window")
    return DialDecision(True)


# --- inbound routing -------------------------------------------------------------


@dataclass(frozen=True)
class InboundRoute:
    kind: str  # forward | ivr | queue | ring_group | ai_agent
    forward_to: str | None = None
    menu: IvrMenu | None = None
    queue: CallQueue | None = None
    ring_group: RingGroup | None = None
    ai_agent_id: uuid.UUID | None = None
    open_now: bool = True


async def _get_scoped(db: AsyncSession, model, tenant_id: uuid.UUID, obj_id):
    try:
        obj_uuid = obj_id if isinstance(obj_id, uuid.UUID) else uuid.UUID(str(obj_id))
    except (ValueError, TypeError):
        return None
    obj = await db.get(model, obj_uuid)
    if obj is None or obj.tenant_id != tenant_id:
        return None
    return obj


async def resolve_inbound_route(
    db: AsyncSession, tenant_id: uuid.UUID, number: str, now: datetime | None = None
) -> InboundRoute | None:
    """The special handling (if any) for an inbound call to `number`. Returns
    None when nothing is configured, so the caller falls back to the plain
    AI-agent behaviour unchanged."""
    if not await table_exists(db, "ivr_menus"):
        return None

    open_now = await is_open_now(db, tenant_id, now)

    rules = (
        (
            await db.execute(
                select(CallForwardingRule)
                .where(
                    CallForwardingRule.tenant_id == tenant_id,
                    CallForwardingRule.number == number,
                    CallForwardingRule.is_active.is_(True),
                )
                .order_by(CallForwardingRule.priority, CallForwardingRule.created_at)
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        if rule.condition == "always" or (rule.condition == "after_hours" and not open_now):
            if rule.destination_type == "number":
                return InboundRoute("forward", forward_to=rule.destination, open_now=open_now)
            if rule.destination_type == "ring_group":
                group = await _get_scoped(db, RingGroup, tenant_id, rule.destination)
                if group:
                    return InboundRoute("ring_group", ring_group=group, open_now=open_now)
            if rule.destination_type == "queue":
                queue = await _get_scoped(db, CallQueue, tenant_id, rule.destination)
                if queue:
                    return InboundRoute("queue", queue=queue, open_now=open_now)

    routing = (
        await db.execute(
            select(NumberRouting).where(
                NumberRouting.tenant_id == tenant_id, NumberRouting.e164_number == number
            )
        )
    ).scalar_one_or_none()
    if routing is not None and routing.target_id is not None:
        if routing.target_type == "ivr":
            menu = await _get_scoped(db, IvrMenu, tenant_id, routing.target_id)
            if menu and menu.is_active:
                return InboundRoute("ivr", menu=menu, open_now=open_now)
        elif routing.target_type == "queue":
            queue = await _get_scoped(db, CallQueue, tenant_id, routing.target_id)
            if queue:
                return InboundRoute("queue", queue=queue, open_now=open_now)
        elif routing.target_type == "ring_group":
            group = await _get_scoped(db, RingGroup, tenant_id, routing.target_id)
            if group:
                return InboundRoute("ring_group", ring_group=group, open_now=open_now)
        elif routing.target_type == "ai_agent":
            return InboundRoute("ai_agent", ai_agent_id=routing.target_id, open_now=open_now)

    menu = (
        (
            await db.execute(
                select(IvrMenu)
                .where(
                    IvrMenu.tenant_id == tenant_id,
                    IvrMenu.number == number,
                    IvrMenu.is_active.is_(True),
                )
                .order_by(IvrMenu.created_at)
            )
        )
        .scalars()
        .first()
    )
    if menu is not None:
        return InboundRoute("ivr", menu=menu, open_now=open_now)
    return None


def _menu_twiml(menu: IvrMenu, call: Call, *, attempt: int = 0, prefix: str = "") -> str:
    url = f"{base_url()}/ivr/{menu.id}/digit?call_id={call.id}&attempt={attempt}"
    return ivr.build_menu_twiml(
        greeting=f"{prefix}{menu.greeting_text}",
        language=menu.language,
        digit_url=url,
        timeout_seconds=menu.timeout_seconds,
    )


async def twiml_for_route(db: AsyncSession, route: InboundRoute, call: Call) -> str:
    if route.kind == "forward":
        return ivr.build_forward_twiml(route.forward_to or "", caller_id=call.to_number)
    if route.kind == "ivr" and route.menu is not None:
        menu = route.menu
        if not route.open_now:
            if menu.after_hours_action:
                return await action_twiml(
                    db, menu.after_hours_action, call=call, language=menu.language
                )
            if menu.after_hours_message:
                return ivr.build_say_hangup_twiml(menu.after_hours_message, menu.language)
        return _menu_twiml(menu, call)
    if route.kind == "queue" and route.queue is not None:
        return await _queue_twiml(db, route.queue, call)
    if route.kind == "ring_group" and route.ring_group is not None:
        return _ring_group_twiml(route.ring_group, call)
    if route.kind == "ai_agent" and route.ai_agent_id is not None:
        return await action_twiml(
            db, {"action": "ai_agent", "target_id": str(route.ai_agent_id)}, call=call
        )
    return ivr.build_say_hangup_twiml()


def _ring_group_twiml(group: RingGroup, call: Call, index: int = 0) -> str:
    return ivr.build_ring_group_twiml(
        list(group.members or []),
        strategy=group.strategy,
        timeout=group.timeout_seconds,
        caller_id=call.to_number,
        next_url=f"{base_url()}/ring-groups/{group.id}/next?call_id={call.id}",
        index=index,
    )


async def ring_queue_members(db: AsyncSession, queue: CallQueue, call: Call) -> int:
    """Rings the queue's member numbers (via Twilio REST) so one of them can
    dequeue the caller. Returns the number of legs started; failures are
    logged, never raised into the caller's webhook."""
    if not queue.members:
        return 0
    settings = get_settings()
    agent_url = f"{base_url()}/queues/{queue.id}/agent?call_id={call.id}"
    started = 0
    try:
        credentials = await get_twilio_credentials(db, call.tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("could not load twilio credentials for queue ring")
        return 0
    for member in list(queue.members)[: ivr.MAX_RING_MEMBERS]:
        try:
            place_outbound_call(
                to=member,
                from_=call.to_number or settings.transfer_fallback_number or "",
                voice_webhook_url=agent_url,
                credentials=credentials,
            )
            started += 1
        except (TwilioClientError, TwilioException) as exc:
            logger.warning("queue ring failed for %s: %s", member, exc)
    return started


async def _queue_twiml(db: AsyncSession, queue: CallQueue, call: Call) -> str:
    await ring_queue_members(db, queue, call)
    name = ivr.twilio_queue_name(str(call.tenant_id), str(queue.id))
    return ivr.build_enqueue_twiml(
        queue_name=name,
        wait_url=f"{base_url()}/queues/{queue.id}/wait",
        result_url=f"{base_url()}/queues/{queue.id}/result?call_id={call.id}",
    )


async def action_twiml(
    db: AsyncSession, action: dict, *, call: Call, language: str = "en-IN", depth: int = 0
) -> str:
    """Executes an IVR/overflow/after-hours action dict for a live call."""
    kind = action.get("action")
    tenant_id = call.tenant_id

    if kind == "ai_agent":
        agent = await _get_scoped(db, AIAgent, tenant_id, action.get("target_id"))
        if agent is None:
            return ivr.build_say_hangup_twiml(
                "Sorry, this service is unavailable right now.", language
            )
        call.ai_agent_id = agent.id
        await db.flush()
        return ivr.build_stream_twiml(
            stream_url=get_settings().realtime_ws_url,
            call_id=str(call.id),
            tenant_id=str(tenant_id),
        )
    if kind == "queue":
        queue = await _get_scoped(db, CallQueue, tenant_id, action.get("target_id"))
        if queue is None:
            return ivr.build_say_hangup_twiml("Sorry, nobody is available.", language)
        return await _queue_twiml(db, queue, call)
    if kind == "ring_group":
        group = await _get_scoped(db, RingGroup, tenant_id, action.get("target_id"))
        if group is None:
            return ivr.build_say_hangup_twiml("Sorry, nobody is available.", language)
        return _ring_group_twiml(group, call)
    if kind == "forward":
        return ivr.build_forward_twiml(str(action.get("number", "")), caller_id=call.to_number)
    if kind == "voicemail":
        return ivr.build_voicemail_twiml(
            message=action.get("message")
            or "Nobody is available right now. Please leave a message after the beep.",
            language=language,
            recording_callback_url=(
                f"{get_settings().public_base_url}/api/v1/webhooks/telephony/twilio/recording"
            ),
        )
    if kind == "submenu" and depth < 5:
        menu = await _get_scoped(db, IvrMenu, tenant_id, action.get("target_id"))
        if menu is not None and menu.is_active:
            return _menu_twiml(menu, call)
    return ivr.build_say_hangup_twiml(action.get("message"), language)


async def handle_digit(
    db: AsyncSession, menu: IvrMenu, call: Call, digits: str | None, attempt: int
) -> str:
    action = ivr.resolve_digit(menu.options or {}, digits)
    if action is not None:
        return await action_twiml(db, action, call=call, language=menu.language)
    if attempt >= menu.max_retries:
        return ivr.build_say_hangup_twiml("Sorry, we did not get a valid choice. Goodbye.", menu.language)
    prefix = "Sorry, that is not a valid choice. " if digits else ""
    return _menu_twiml(menu, call, attempt=attempt + 1, prefix=prefix)


async def handle_queue_result(
    db: AsyncSession, queue: CallQueue, call: Call, result: str | None
) -> str:
    """<Enqueue action>: runs when the caller leaves the queue. 'bridged' and
    'hangup' mean the call is over/connected; anything else is overflow."""
    if result in ("bridged", "hangup", "error"):
        return ivr.build_say_hangup_twiml() if result != "error" else ivr.build_say_hangup_twiml(
            "Sorry, we are unable to connect your call.", "en-IN"
        )
    overflow = queue.overflow_action or {"action": "voicemail"}
    return await action_twiml(db, overflow, call=call)


# --- dispositions / stats ------------------------------------------------------------


async def get_dialer_settings(
    db: AsyncSession, campaign_id: uuid.UUID
) -> CampaignDialerSettings | None:
    if not await table_exists(db, "campaign_dialer_settings"):
        return None
    return (
        await db.execute(
            select(CampaignDialerSettings).where(CampaignDialerSettings.campaign_id == campaign_id)
        )
    ).scalar_one_or_none()


async def get_or_create_state(
    db: AsyncSession, campaign: Campaign, contact: CampaignContact
) -> CampaignContactState:
    state = (
        await db.execute(
            select(CampaignContactState).where(CampaignContactState.contact_id == contact.id)
        )
    ).scalar_one_or_none()
    if state is None:
        state = CampaignContactState(
            tenant_id=campaign.tenant_id, campaign_id=campaign.id, contact_id=contact.id, attempts=0
        )
        db.add(state)
        await db.flush()
    return state


async def _route_qualified(
    db: AsyncSession,
    campaign: Campaign,
    customer: Customer | None,
    *,
    owner_user_id: uuid.UUID | None,
    call_id: uuid.UUID | None,
) -> dict | None:
    """A qualified lead goes to the CRM when that module exists at runtime
    (model importable AND table present), otherwise to a high-priority ticket."""
    if customer is None:
        return None
    try:
        crm = importlib.import_module("packages.database.models.crm")
        lead_model = crm.CrmLead
    except (ImportError, AttributeError):
        lead_model = None

    if lead_model is not None and await table_exists(db, "crm_leads"):
        lead = lead_model(
            tenant_id=campaign.tenant_id,
            name=customer.name,
            email=customer.email,
            phone=customer.phone,
            customer_id=customer.id,
            owner_user_id=owner_user_id,
            source_call_id=call_id,
            source=f"campaign:{campaign.name}"[:80],
        )
        db.add(lead)
        await db.flush()
        return {"type": "crm_lead", "id": str(lead.id)}

    ticket = Ticket(
        tenant_id=campaign.tenant_id,
        customer_id=customer.id,
        assigned_to_user_id=owner_user_id,
        subject=f"Qualified lead from campaign '{campaign.name}'"[:255],
        priority=TicketPriority.HIGH,
    )
    db.add(ticket)
    await db.flush()
    from app.services.operations import apply_sla_safely

    await apply_sla_safely(db, campaign.tenant_id, ticket)
    return {"type": "ticket", "id": str(ticket.id)}


async def record_disposition(
    db: AsyncSession,
    campaign: Campaign,
    contact: CampaignContact,
    *,
    disposition: str,
    settings_row: CampaignDialerSettings | None,
    source: str = "manual",
    notes: str | None = None,
    callback_at: datetime | None = None,
    qualified: bool = False,
    call_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> CampaignOutcome:
    """Stores the outcome and moves the contact: connected -> completed,
    callback -> pending at callback_at, retryable -> pending after the retry
    schedule (or failed once attempts are exhausted), dnc -> DND registry."""
    now = datetime.now(UTC)
    qualified = qualified or disposition == "qualified"
    state = await get_or_create_state(db, campaign, contact)
    state.last_disposition = disposition
    if call_id is not None:
        state.last_processed_call_id = call_id

    outcome = CampaignOutcome(
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        call_id=call_id,
        disposition=disposition,
        source=source,
        qualified=qualified,
        notes=notes,
        callback_at=callback_at,
    )
    db.add(outcome)

    max_attempts = settings_row.max_attempts if settings_row else 3
    schedule = list(settings_row.retry_schedule_minutes) if settings_row else [60]
    result = dict(contact.result or {})
    result["disposition"] = disposition

    if disposition == "callback":
        when = callback_at or now + timedelta(hours=1)
        state.next_attempt_at = when
        contact.status = CampaignContactStatus.PENDING
    elif disposition in RETRYABLE:
        if state.attempts < max_attempts:
            state.next_attempt_at = now + timedelta(
                minutes=dialer.retry_delay_minutes(schedule, state.attempts)
            )
            contact.status = CampaignContactStatus.PENDING
        else:
            state.next_attempt_at = None
            contact.status = CampaignContactStatus.FAILED
    else:
        state.next_attempt_at = None
        contact.status = CampaignContactStatus.COMPLETED

    customer = await db.get(Customer, contact.customer_id)
    if disposition == "dnc" and customer is not None and customer.phone:
        exists = (
            await db.execute(
                select(DndEntry.id).where(
                    DndEntry.tenant_id == campaign.tenant_id,
                    DndEntry.e164_number == customer.phone,
                )
            )
        ).first()
        if not exists:
            db.add(
                DndEntry(
                    tenant_id=campaign.tenant_id,
                    e164_number=customer.phone,
                    source="campaign_dnc",
                    note=f"campaign {campaign.name}",
                )
            )

    if qualified and "lead" not in result:
        lead = await _route_qualified(
            db,
            campaign,
            customer,
            owner_user_id=settings_row.route_qualified_to_user_id if settings_row else user_id,
            call_id=call_id,
        )
        if lead:
            result["lead"] = lead

    contact.result = result
    await db.flush()
    return outcome


_AUTO_DISPOSITION = {
    CallStatus.COMPLETED: "answered",
    CallStatus.NO_ANSWER: "no_answer",
    CallStatus.FAILED: "failed",
}


async def reconcile_finished_calls(
    db: AsyncSession, campaign: Campaign, settings_row: CampaignDialerSettings | None
) -> int:
    """Turns terminal calls into auto dispositions (once per call) so retries
    and stats work without an agent touching every contact."""
    rows = (
        await db.execute(
            select(CampaignContactState, CampaignContact)
            .join(CampaignContact, CampaignContact.id == CampaignContactState.contact_id)
            .where(
                CampaignContactState.campaign_id == campaign.id,
                CampaignContactState.last_call_id.is_not(None),
            )
        )
    ).all()
    processed = 0
    for state, contact in rows:
        if state.last_call_id == state.last_processed_call_id:
            continue
        call = await db.get(Call, state.last_call_id)
        if call is None or call.status not in _AUTO_DISPOSITION:
            continue
        already = (
            await db.execute(
                select(CampaignOutcome.id).where(
                    CampaignOutcome.contact_id == contact.id,
                    CampaignOutcome.call_id == call.id,
                )
            )
        ).first()
        if already:
            state.last_processed_call_id = call.id
            continue
        await record_disposition(
            db,
            campaign,
            contact,
            disposition=_AUTO_DISPOSITION[call.status],
            settings_row=settings_row,
            source="auto",
            call_id=call.id,
        )
        processed += 1
    return processed


async def dialer_snapshot(
    db: AsyncSession, campaign: Campaign, settings_row: CampaignDialerSettings, now: datetime
) -> dict:
    """Live numbers next_batch needs."""
    call_rows = (
        await db.execute(
            select(Call.status, func.count())
            .join(CampaignContactState, CampaignContactState.last_call_id == Call.id)
            .where(CampaignContactState.campaign_id == campaign.id)
            .group_by(Call.status)
        )
    ).all()
    by_status = {status: count for status, count in call_rows}
    ringing = by_status.get(CallStatus.QUEUED, 0) + by_status.get(CallStatus.RINGING, 0)
    in_progress = by_status.get(CallStatus.IN_PROGRESS, 0)

    outcome_rows = (
        await db.execute(
            select(CampaignOutcome.disposition, func.count())
            .where(CampaignOutcome.campaign_id == campaign.id)
            .group_by(CampaignOutcome.disposition)
        )
    ).all()
    counts = {disp: n for disp, n in outcome_rows}
    attempts = sum(counts.values())
    answered = sum(n for d, n in counts.items() if d in CONNECTED)
    answer_rate, abandon_rate = dialer.answer_and_abandon_rates(
        attempts=attempts, answered=answered, abandoned=counts.get("abandoned", 0)
    )

    daily_remaining = None
    if settings_row.daily_call_limit is not None:
        zone = dialer.safe_zone(settings_row.default_timezone)
        local_midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        dialed_today = (
            await db.execute(
                select(func.count())
                .select_from(Call)
                .join(Customer, Customer.id == Call.customer_id)
                .join(CampaignContact, CampaignContact.customer_id == Customer.id)
                .where(
                    CampaignContact.campaign_id == campaign.id,
                    Call.tenant_id == campaign.tenant_id,
                    Call.direction == CallDirection.OUTBOUND,
                    Call.created_at >= local_midnight,
                )
            )
        ).scalar_one()
        daily_remaining = max(settings_row.daily_call_limit - dialed_today, 0)

    return {
        "ringing": ringing,
        "in_progress": in_progress,
        "answer_rate": answer_rate,
        "abandon_rate": abandon_rate,
        "daily_remaining": daily_remaining,
    }


async def eligible_contacts(
    db: AsyncSession, campaign: Campaign, now: datetime, limit: int = 500
) -> list[tuple[CampaignContact, CampaignContactState | None]]:
    rows = (
        await db.execute(
            select(CampaignContact, CampaignContactState)
            .outerjoin(CampaignContactState, CampaignContactState.contact_id == CampaignContact.id)
            .where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.status == CampaignContactStatus.PENDING,
            )
            .order_by(CampaignContact.created_at)
            .limit(limit)
        )
    ).all()
    return [
        (contact, state)
        for contact, state in rows
        if state is None or state.next_attempt_at is None or state.next_attempt_at <= now
    ]


async def campaign_stats(db: AsyncSession, campaign: Campaign) -> dict:
    status_rows = (
        await db.execute(
            select(CampaignContact.status, func.count())
            .where(CampaignContact.campaign_id == campaign.id)
            .group_by(CampaignContact.status)
        )
    ).all()
    contacts_by_status = {s.value: n for s, n in status_rows}

    disp_rows = (
        await db.execute(
            select(CampaignOutcome.disposition, func.count())
            .where(CampaignOutcome.campaign_id == campaign.id)
            .group_by(CampaignOutcome.disposition)
        )
    ).all()
    dispositions = {d: n for d, n in disp_rows}
    attempts = sum(dispositions.values())
    answered = sum(n for d, n in dispositions.items() if d in CONNECTED)
    qualified = (
        await db.execute(
            select(func.count())
            .select_from(CampaignOutcome)
            .where(CampaignOutcome.campaign_id == campaign.id, CampaignOutcome.qualified.is_(True))
        )
    ).scalar_one()
    avg_duration = (
        await db.execute(
            select(func.avg(Call.duration_seconds))
            .join(CampaignOutcome, CampaignOutcome.call_id == Call.id)
            .where(CampaignOutcome.campaign_id == campaign.id, Call.duration_seconds.is_not(None))
        )
    ).scalar_one()
    scheduled_retries = (
        await db.execute(
            select(func.count())
            .select_from(CampaignContactState)
            .where(
                CampaignContactState.campaign_id == campaign.id,
                CampaignContactState.next_attempt_at.is_not(None),
            )
        )
    ).scalar_one()
    total = sum(contacts_by_status.values())
    return {
        "campaign_id": str(campaign.id),
        "total_contacts": total,
        "contacts_by_status": contacts_by_status,
        "attempts": attempts,
        "dispositions": dispositions,
        "answered": answered,
        "answer_rate": round(answered / attempts, 4) if attempts else 0.0,
        "abandon_rate": round(dispositions.get("abandoned", 0) / answered, 4) if answered else 0.0,
        "qualified": qualified,
        "qualified_rate": round(qualified / answered, 4) if answered else 0.0,
        "avg_call_seconds": round(float(avg_duration), 1) if avg_duration is not None else None,
        "scheduled_retries": scheduled_retries,
        "completion_rate": round(
            (contacts_by_status.get("completed", 0) + contacts_by_status.get("failed", 0)) / total,
            4,
        )
        if total
        else 0.0,
    }
