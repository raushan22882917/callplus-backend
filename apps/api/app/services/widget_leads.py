"""Website chat leads: capture the visitor's details, and (with their consent)
have the AI agent phone them. Everything here is reachable by anonymous
visitors, so each step is limited: consent is required and recorded, the number
must be valid, a number can only be requested a couple of times a day, and the
business has a daily cap (plus the normal blocklist, DND and cost controls)."""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from app.services.channels import find_or_create_customer_by_email
from app.services.customer_lookup import find_or_create_customer_by_phone
from app.services.outbound_call import OutboundCallError, place_ai_outbound_call
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AgentStatus,
    AIAgent,
    Call,
    CallStatus,
    ChannelSettings,
    Customer,
    PhoneNumber,
    Tenant,
)
from packages.database.models.crm import CrmLead, CrmNote, CrmStage, CrmTask, CrmTaskType
from packages.database.models.widget_lead import WidgetLead

logger = logging.getLogger(__name__)

DEFAULT_CONSENT = "I agree that {business} may call me on this number about my enquiry."
DEFAULT_MAX_CALLS_PER_DAY = 50
# The same number can be called back this many times in 24 hours, so a visitor
# can't use the form to ring someone else repeatedly.
MAX_REQUESTS_PER_NUMBER_PER_DAY = 2
# A request made again within this window just returns the call already placed.
DUPLICATE_WINDOW = timedelta(minutes=10)
SHORT_CALL_SECONDS = 10

DIAL_CODES = {
    "IN": "91", "US": "1", "CA": "1", "GB": "44", "AE": "971", "SG": "65", "AU": "61",
    "NZ": "64", "SA": "966", "QA": "974", "KW": "965", "OM": "968", "BH": "973", "NP": "977",
    "BD": "880", "LK": "94", "PK": "92", "ZA": "27", "DE": "49", "FR": "33", "NG": "234",
    "KE": "254", "MY": "60", "ID": "62", "PH": "63", "MX": "52", "BR": "55",
}  # fmt: skip

_CALL_WORDS = re.compile(
    r"\b(call\s+me|give\s+me\s+a\s+call|ring\s+me|phone\s+me|call\s+back|callback|call\s+karo|call\s+kar\s+do|"
    r"can\s+(you|someone|somebody)\s+call|please\s+call|want\s+(a|to\s+get\s+a)\s+call|"
    r"talk\s+to\s+(someone|a\s+person)\s+on\s+(the\s+)?(phone|call))\b"
    r"|मुझे\s+(कॉल|फोन)\s+कर|फोन\s+करो|कॉल\s+करो|कॉल\s+कर\s+दो",
    re.IGNORECASE,
)
_NEGATED_CALL = re.compile(r"\b(don'?t|do\s+not|dont|no\s+need\s+to|never)\b.{0,20}\bcall\b", re.IGNORECASE)


class LeadError(Exception):
    """Something the visitor can fix, or must be told, with the HTTP status to use."""

    def __init__(self, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def wants_call(text: str) -> bool:
    """The visitor is asking for a phone call in their chat message."""
    return bool(text) and _CALL_WORDS.search(text) is not None and _NEGATED_CALL.search(text) is None


def normalize_visitor_phone(raw: str | None, default_country: str | None = None) -> str | None:
    """E.164 from whatever the visitor typed, or None if they typed nothing.
    Raises LeadError for something that cannot be a phone number."""
    text = (raw or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"[+\d\s\-().]{6,30}", text):
        raise LeadError("That phone number doesn't look right.")
    digits = re.sub(r"\D", "", text)
    code = DIAL_CODES.get((default_country or "").upper())
    if text.lstrip("( ").startswith("+"):
        number = digits
    elif digits.startswith("00"):
        number = digits[2:]
    elif code is not None:
        local = digits.lstrip("0")
        number = digits if (digits.startswith(code) and len(local) > 10) else code + local
    else:
        raise LeadError("Please include your country code, like +91 98765 43210.")
    if not 8 <= len(number) <= 15 or number.startswith("0"):
        raise LeadError("That phone number doesn't look right.")
    return f"+{number}"


def country_allowed(phone: str, allowed: list[str] | None) -> bool:
    """`allowed` holds calling codes like "+91"; empty or missing means anywhere."""
    prefixes = [a.strip() for a in (allowed or []) if a and a.strip()]
    return not prefixes or any(phone.startswith(p if p.startswith("+") else f"+{p}") for p in prefixes)


def consent_text_for(config: dict, business: str) -> str:
    template = (config.get("consent_text") or DEFAULT_CONSENT).strip()
    return template.replace("{business}", business)[:600]


WIDGET_TEMPLATES = ("modern", "minimal", "bold", "soft")


def widget_options(config: dict, business: str) -> dict:
    """What the widget needs to know, safe to show any visitor."""
    template = config.get("template")
    return {
        "lead_form": bool(config.get("lead_form")),
        "require_phone": bool(config.get("require_phone")),
        "call_me": bool(config.get("call_me")),
        "consent_text": consent_text_for(config, business),
        "template": template if template in WIDGET_TEMPLATES else "modern",
        "subtitle": str(config.get("subtitle") or "")[:80],
        "position": "left" if config.get("position") == "left" else "right",
        "launcher_label": str(config.get("launcher_label") or "")[:30],
        "lead_heading": str(config.get("lead_heading") or "")[:80],
        "lead_intro": str(config.get("lead_intro") or "")[:200],
        "button_label": str(config.get("button_label") or "")[:30],
        "show_email": config.get("show_email") is not False,
        "show_message": config.get("show_message") is not False,
        "quick_replies": [
            q.strip()[:60]
            for q in (config.get("quick_replies") or [])
            if isinstance(q, str) and q.strip()
        ][:4],
    }


async def _upsert_crm_lead(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer: Customer,
    *,
    name: str | None,
    phone: str | None,
    email: str | None,
    interest: str | None,
    page_url: str | None,
) -> CrmLead:
    conditions = []
    if phone:
        conditions.append(CrmLead.phone == phone)
    if email:
        conditions.append(func.lower(CrmLead.email) == email.lower())
    existing = None
    if conditions:
        existing = (
            await db.execute(
                select(CrmLead)
                .where(CrmLead.tenant_id == tenant_id, or_(*conditions))
                .order_by(CrmLead.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    detail = " · ".join(x for x in (f"Wants: {interest}" if interest else "", page_url or "") if x)
    if existing is not None:
        existing.email = existing.email or email
        existing.phone = existing.phone or phone
        existing.customer_id = existing.customer_id or customer.id
        db.add(CrmNote(tenant_id=tenant_id, lead_id=existing.id, kind="system", body=f"Came back through website chat. {detail}".strip()))
        return existing
    lead = CrmLead(
        tenant_id=tenant_id,
        name=(name or phone or email or "Website visitor")[:255],
        email=email,
        phone=phone,
        customer_id=customer.id,
        source="web_chat",
        tags=["web_chat"],
        custom_fields={k: v for k, v in {"interest": interest, "page_url": page_url}.items() if v},
    )
    db.add(lead)
    await db.flush()
    db.add(CrmNote(tenant_id=tenant_id, lead_id=lead.id, kind="system", body=f"New lead from website chat. {detail}".strip()))
    try:
        from app.services import crm as crm_service

        await crm_service.rescore_lead(db, lead)
        await crm_service.push_lead_to_connected_crms(db, lead)
    except Exception:
        logger.warning("scoring/pushing web chat lead %s failed", lead.id, exc_info=True)
    return lead


async def capture_lead(
    db: AsyncSession,
    row: ChannelSettings,
    *,
    conversation_id: uuid.UUID,
    name: str | None,
    phone: str | None,
    email: str | None,
    interest: str | None,
    page_url: str | None,
    referrer: str | None,
    utm: dict | None,
    user_agent: str | None,
    ip: str | None,
    answers: list[dict] | None = None,
    require_contact: bool = True,
) -> tuple[WidgetLead, Customer | None]:
    """Stores the visitor as a customer, a CRM lead and a widget lead (the
    consent and attribution record). Needs at least a phone or an email, unless
    the business's own form has no way to ask for either (`require_contact` False)."""
    if not phone and not email and require_contact:
        raise LeadError("Please give us a phone number or an email address.")
    if phone:
        customer, _ = await find_or_create_customer_by_phone(db, row.tenant_id, phone)
        if email and not customer.email:
            customer.email = email
    elif email:
        customer, _ = await find_or_create_customer_by_email(db, row.tenant_id, email, name)
    else:
        # No contact detail to match on: this visit is its own customer.
        customer = Customer(tenant_id=row.tenant_id, name=(name or "Website visitor")[:255])
        db.add(customer)
        await db.flush()
    if name and (customer.name in (customer.phone, customer.email)):
        customer.name = name[:255]

    crm_lead = await _upsert_crm_lead(
        db, row.tenant_id, customer, name=name, phone=phone, email=email, interest=interest, page_url=page_url
    )
    if answers:
        # Visible on the CRM lead too, under their own labels.
        crm_lead.custom_fields = {**(crm_lead.custom_fields or {}), "chat_form": {a["label"]: a["value"] for a in answers}}
    lead = WidgetLead(
        tenant_id=row.tenant_id,
        conversation_id=conversation_id,
        customer_id=customer.id,
        crm_lead_id=crm_lead.id,
        name=(name or None) and name[:255],
        phone=phone,
        email=email,
        interest=(interest or None) and interest[:2000],
        answers=list(answers or []),
        page_url=(page_url or None) and page_url[:2048],
        referrer=(referrer or None) and referrer[:2048],
        utm={k: str(v)[:200] for k, v in (utm or {}).items() if v},
        user_agent=(user_agent or None) and user_agent[:255],
        ip_address=ip,
        call_status="none",
    )
    db.add(lead)
    await db.flush()
    return lead, customer


async def _calls_requested_today(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        await db.execute(
            select(func.count(WidgetLead.id)).where(
                WidgetLead.tenant_id == tenant_id, WidgetLead.call_requested_at >= since
            )
        )
    ).scalar_one()


async def _requests_for_number(db: AsyncSession, tenant_id: uuid.UUID, phone: str) -> int:
    since = datetime.now(UTC) - timedelta(hours=24)
    return (
        await db.execute(
            select(func.count(WidgetLead.id)).where(
                WidgetLead.tenant_id == tenant_id,
                WidgetLead.phone == phone,
                WidgetLead.call_requested_at >= since,
            )
        )
    ).scalar_one()


async def _follow_up_task(db: AsyncSession, lead: WidgetLead, reason: str) -> None:
    """When the AI cannot place the call, a person is asked to."""
    if lead.crm_lead_id is None:
        return
    db.add(
        CrmTask(
            tenant_id=lead.tenant_id,
            lead_id=lead.crm_lead_id,
            customer_id=lead.customer_id,
            type=CrmTaskType.CALL,
            title=f"Call back {lead.name or lead.phone} (asked on website chat)",
            description=f"{reason}. They agreed to be called on {lead.phone}.",
            due_at=datetime.now(UTC),
            created_by_ai=True,
        )
    )


async def request_call(
    db: AsyncSession,
    row: ChannelSettings,
    lead: WidgetLead,
    *,
    phone: str | None,
    consent: bool,
    note: str | None,
    ip: str | None,
) -> dict:
    """The visitor asks to be phoned. Returns {"status": "calling"|"queued", ...};
    raises LeadError for what they should fix or wait for."""
    config = row.config or {}
    if not config.get("call_me"):
        raise LeadError("Phone calls aren't available right now.", 404)
    if not consent:
        raise LeadError("Please tick the box to agree to a call.")
    tenant = await db.get(Tenant, row.tenant_id)
    number = normalize_visitor_phone(phone, tenant.country if tenant else None) or lead.phone
    if not number:
        raise LeadError("Please enter the phone number we should call.")
    if not country_allowed(number, config.get("allowed_countries")):
        raise LeadError("Sorry, we can't call numbers from your country. Please use the chat instead.")

    # Already on its way: don't ring twice.
    now = datetime.now(UTC)
    if (
        lead.call_status in ("requested", "calling")
        and lead.call_requested_at is not None
        and now - lead.call_requested_at < DUPLICATE_WINDOW
    ):
        return {"status": "calling", "message": "We're calling you now. Please keep your phone nearby."}

    if await _requests_for_number(db, row.tenant_id, number) >= MAX_REQUESTS_PER_NUMBER_PER_DAY:
        raise LeadError("We've already called this number today. We'll be in touch soon.", 429)
    cap = int(config.get("max_calls_per_day") or DEFAULT_MAX_CALLS_PER_DAY)
    if await _calls_requested_today(db, row.tenant_id) >= cap:
        lead.phone = number
        await _record_consent(db, row, lead, tenant, note, ip, now)
        lead.call_status = "requested"
        lead.last_call_error = "Daily limit for website call requests reached"
        await _follow_up_task(db, lead, "The daily limit for website call requests was reached")
        await db.commit()
        return {"status": "queued", "message": "Thanks! Our team will call you shortly."}

    lead.phone = number
    customer = await db.get(Customer, lead.customer_id) if lead.customer_id else None
    if customer is not None and not customer.phone:
        customer.phone = number
    await _record_consent(db, row, lead, tenant, note, ip, now)
    lead.call_attempts += 1
    lead.call_requested_at = now
    lead.last_call_error = None
    # Consent and the request are saved before dialling, so they survive a failed call.
    await db.commit()
    # Read now: after a rollback these objects are expired and cannot be read.
    lead_id, tenant_id, ai_agent_id = lead.id, row.tenant_id, row.ai_agent_id
    crm_lead_id = lead.crm_lead_id
    config = dict(row.config or {})

    try:
        agent_id, from_number = await choose_call_setup(
            db, tenant_id, channel_agent_id=ai_agent_id, config=config, to_number=number
        )
        call = await place_ai_outbound_call(
            db,
            tenant_id,
            to_number=number,
            ai_agent_id=str(agent_id),
            from_number=from_number,
        )
    except OutboundCallError as exc:
        # Drops whatever the failed attempt half-created; the consent and the
        # request itself were committed above, so they are kept.
        await db.rollback()
        await db.refresh(row)  # rollback expired it, and the caller still reads it
        lead = await db.get(WidgetLead, lead_id)
        lead.last_call_error = str(exc)[:300]
        lead.call_status = "blocked" if _is_policy_block(str(exc)) else "failed"
        await _follow_up_task(db, lead, f"The AI call could not be placed ({str(exc)[:120]})")
        await db.commit()
        return {"status": "queued", "message": "Thanks! Our team will call you shortly."}

    lead.call_id = call.id
    lead.call_status = "calling"
    if crm_lead_id:
        db.add(
            CrmNote(
                tenant_id=lead.tenant_id,
                lead_id=crm_lead_id,
                kind="system",
                body=f"Asked for a call on website chat; the AI agent is calling {number}."
                + (f" Note: {note}" if note else ""),
            )
        )
    await db.commit()
    return {"status": "calling", "message": "We're calling you now. Please keep your phone nearby.", "call_id": str(call.id)}


def _is_policy_block(message: str) -> bool:
    lowered = message.lower()
    return any(w in lowered for w in ("blocklist", "do-not-disturb", "budget", "daily limit", "outbound calls"))


async def _record_consent(
    db: AsyncSession, row: ChannelSettings, lead: WidgetLead, tenant, note: str | None, ip: str | None, now: datetime
) -> None:
    lead.consent_call = True
    lead.consent_text = consent_text_for(row.config or {}, tenant.name if tenant else "us")
    lead.consent_at = now
    lead.consent_ip = ip
    lead.call_note = (note or "").strip()[:500] or lead.call_note


async def on_call_finished(db: AsyncSession, call: Call) -> None:
    """Called when any call ends; updates the lead if this call came from the widget."""
    lead = (
        await db.execute(select(WidgetLead).where(WidgetLead.call_id == call.id))
    ).scalar_one_or_none()
    if lead is None:
        return
    if call.status == CallStatus.COMPLETED:
        lead.call_status = "completed" if (call.duration_seconds or 0) >= SHORT_CALL_SECONDS else "no_answer"
    elif call.status == CallStatus.NO_ANSWER:
        lead.call_status = "no_answer"
    else:
        lead.call_status = "failed"

    crm_lead = await db.get(CrmLead, lead.crm_lead_id) if lead.crm_lead_id else None
    if crm_lead is not None:
        crm_lead.source_call_id = call.id
        if lead.call_status == "completed":
            if crm_lead.stage == CrmStage.NEW_LEAD:
                from app.services import crm as crm_service

                await crm_service.move_lead(db, crm_lead, CrmStage.CONTACTED)
            db.add(CrmNote(tenant_id=lead.tenant_id, lead_id=crm_lead.id, kind="system", body=f"The AI agent spoke with them for {(call.duration_seconds or 0) // 60}m {(call.duration_seconds or 0) % 60}s."))
        else:
            await _follow_up_task(db, lead, f"The AI call ended as {lead.call_status.replace('_', ' ')}")
    await db.commit()


def calling_code(e164: str) -> str | None:
    """The country calling code at the start of an E.164 number ("+919876543210" -> "91")."""
    digits = (e164 or "").lstrip("+")
    known = set(DIAL_CODES.values())
    return next((digits[:n] for n in (3, 2, 1) if digits[:n] in known), None)


async def choose_call_setup(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    channel_agent_id: uuid.UUID | None,
    config: dict,
    to_number: str,
) -> tuple[uuid.UUID, str]:
    """Which AI agent makes the call, and from which of the business's numbers.

    The platform's own rule is "exactly one number" which fails for any business with
    two, or whose chat has no agent chosen. Here the business never has to think about it:
      * agent: the chat's agent if it is active; else an active agent that owns a number;
        else the oldest active agent.
      * number: the one the business picked in the chat settings (`call_from_number`);
        else one assigned to that agent, else any of its numbers. Among several, a number
        from the visitor's own country comes first (it looks local and is trusted more),
        then the oldest.
    Raises OutboundCallError saying what is missing when there is nothing to call with."""
    numbers = list(
        (await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id).order_by(PhoneNumber.created_at)))
        .scalars()
        .all()
    )
    if not numbers:
        raise OutboundCallError("This business has no phone number to call from. Add one under Phone numbers.")
    agents = list(
        (
            await db.execute(
                select(AIAgent)
                .where(AIAgent.tenant_id == tenant_id, AIAgent.status == AgentStatus.ACTIVE)
                .order_by(AIAgent.created_at)
            )
        )
        .scalars()
        .all()
    )
    # The agent the owner attached to the chat is used as chosen, whatever its status.
    chosen_agent = await db.get(AIAgent, channel_agent_id) if channel_agent_id else None
    if chosen_agent is not None and chosen_agent.tenant_id != tenant_id:
        chosen_agent = None
    if chosen_agent is None and not agents:
        raise OutboundCallError("This business has no active AI agent to make the call.")
    owners = {n.ai_agent_id for n in numbers}
    agent = chosen_agent or next((a for a in agents if a.id in owners), agents[0])

    chosen = str(config.get("call_from_number") or "").strip()
    pool = [n for n in numbers if n.e164_number == chosen] if chosen else []
    if not pool:
        pool = [n for n in numbers if n.ai_agent_id == agent.id] or numbers
    visitor_code = calling_code(to_number)
    best = sorted(pool, key=lambda n: (calling_code(n.e164_number) != visitor_code, n.created_at))[0]
    return agent.id, best.e164_number


async def call_lead(
    db: AsyncSession, lead: WidgetLead, ai_agent_id: uuid.UUID | None, config: dict | None = None
) -> Call:
    """A team member asks the AI agent to call (or retry) a lead. Only leads who
    agreed to a call can be dialled."""
    if not lead.consent_call:
        raise LeadError("This visitor has not agreed to a call.", 409)
    if not lead.phone:
        raise LeadError("This lead has no phone number.", 409)
    now = datetime.now(UTC)
    if (
        lead.call_status == "calling"
        and lead.call_requested_at is not None
        and now - lead.call_requested_at < DUPLICATE_WINDOW
    ):
        raise LeadError("A call to this lead is already in progress.", 409)
    lead.call_attempts += 1
    lead.call_requested_at = now
    lead.last_call_error = None
    await db.commit()
    lead_id, tenant_id, phone = lead.id, lead.tenant_id, lead.phone  # readable after a rollback
    try:
        agent_id, from_number = await choose_call_setup(
            db, tenant_id, channel_agent_id=ai_agent_id, config=config or {}, to_number=phone
        )
        call = await place_ai_outbound_call(
            db, tenant_id, to_number=phone, ai_agent_id=str(agent_id), from_number=from_number
        )
    except OutboundCallError as exc:
        await db.rollback()
        lead = await db.get(WidgetLead, lead_id)
        lead.last_call_error = str(exc)[:300]
        lead.call_status = "blocked" if _is_policy_block(str(exc)) else "failed"
        await db.commit()
        raise LeadError(str(exc), 409) from exc
    lead.call_id = call.id
    lead.call_status = "calling"
    await db.commit()
    return call
