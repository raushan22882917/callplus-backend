"""WhatsApp message templates: write, check, get Meta approval, send.

WhatsApp only allows a business to message someone who hasn't written in the
last 24 hours with a template Meta has approved. Businesses write templates with
named blanks ("Hi {{name}}, your {{service}} is on {{date}}"); Meta wants
numbered ones ({{1}}, {{2}}) plus example values. We convert, remember the order
in `variables`, and fill the blanks from the customer / booking when sending.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.integrations.whatsapp import WhatsAppAPIError, friendly_error
from app.services.plans import PlanLimitError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, Tenant, WhatsAppAccount, WhatsAppTemplate

logger = logging.getLogger(__name__)

CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")
EDITABLE = ("DRAFT", "REJECTED")

# Blanks a business can use without explaining them; anything else is allowed too.
VARIABLES: dict[str, dict[str, str]] = {
    "name": {"label": "Customer name", "example": "Priya"},
    "business": {"label": "Your business name", "example": "Apna Salon"},
    "service": {"label": "Service", "example": "Haircut"},
    "date": {"label": "Date", "example": "Saturday 4 October"},
    "time": {"label": "Time", "example": "2:00 pm"},
    "staff": {"label": "Staff member", "example": "Asha"},
    "amount": {"label": "Amount", "example": "₹300"},
    "link": {"label": "Link", "example": "https://pay.example.com/abc"},
    "code": {"label": "One-time code", "example": "482913"},
    "order_id": {"label": "Order / booking number", "example": "A1023"},
}

# Ready-made starting points. Purpose lets automatic sends find the right one.
LIBRARY: list[dict] = [
    {
        "purpose": "booking_confirmation", "title": "Booking confirmed", "name": "booking_confirmation",
        "category": "UTILITY", "header_text": "Booking confirmed",
        "body": "Hi {{name}}, your {{service}} at {{business}} is booked for {{date}} at {{time}}. Reply here if you need to change it.",
        "footer_text": "Reply STOP to opt out", "buttons": [{"type": "QUICK_REPLY", "text": "Reschedule"}, {"type": "QUICK_REPLY", "text": "Cancel"}],
    },
    {
        "purpose": "booking_reminder", "title": "Booking reminder", "name": "booking_reminder",
        "category": "UTILITY", "header_text": "Reminder",
        "body": "Hi {{name}}, a reminder of your {{service}} at {{business}} on {{date}} at {{time}}. See you soon!",
        "footer_text": None, "buttons": [{"type": "QUICK_REPLY", "text": "I'll be there"}, {"type": "QUICK_REPLY", "text": "Reschedule"}],
    },
    {
        "purpose": "booking_rescheduled", "title": "Booking moved", "name": "booking_rescheduled",
        "category": "UTILITY", "header_text": None,
        "body": "Hi {{name}}, your {{service}} at {{business}} has been moved to {{date}} at {{time}}. Reply here if this doesn't suit you.",
        "footer_text": None, "buttons": [],
    },
    {
        "purpose": "booking_cancelled", "title": "Booking cancelled", "name": "booking_cancelled",
        "category": "UTILITY", "header_text": None,
        "body": "Hi {{name}}, your {{service}} at {{business}} on {{date}} has been cancelled. Reply here to book a new time.",
        "footer_text": None, "buttons": [{"type": "QUICK_REPLY", "text": "Book again"}],
    },
    {
        "purpose": "payment_link", "title": "Payment link", "name": "payment_request",
        "category": "UTILITY", "header_text": "Payment",
        "body": "Hi {{name}}, here is your payment link for {{amount}} at {{business}}: {{link}} Thank you!",
        "footer_text": None, "buttons": [],
    },
    {
        "purpose": "missed_call", "title": "Missed your call", "name": "missed_call_followup",
        "category": "UTILITY", "header_text": None,
        "body": "Hi {{name}}, sorry we missed your call at {{business}}. Reply here and we'll help you right away.",
        "footer_text": None, "buttons": [{"type": "QUICK_REPLY", "text": "Call me back"}],
    },
    {
        "purpose": "feedback", "title": "Feedback request", "name": "feedback_request",
        "category": "MARKETING", "header_text": None,
        "body": "Hi {{name}}, thank you for visiting {{business}}! How was your {{service}}? Reply with a rating from 1 to 5.",
        "footer_text": "Reply STOP to opt out", "buttons": [],
    },
    {
        "purpose": "offer", "title": "Special offer", "name": "special_offer",
        "category": "MARKETING", "header_text": "A treat for you",
        "body": "Hi {{name}}, enjoy 20% off your next {{service}} at {{business}} this week. Reply BOOK to grab a slot.",
        "footer_text": "Reply STOP to opt out", "buttons": [{"type": "QUICK_REPLY", "text": "Book now"}],
    },
]
PURPOSES = {t["purpose"]: t["title"] for t in LIBRARY} | {"custom": "Other"}

_NAMED = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
_NUMBERED = re.compile(r"\{\{\s*(\d+)\s*\}\}")


class TemplateError(Exception):
    pass


def slugify_name(text: str) -> str:
    """Meta template names: lowercase letters, numbers and underscores."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (slug or "template")[:512]


def compile_body(body: str) -> tuple[str, list[str]]:
    """'Hi {{name}}, {{date}} ... {{name}}' -> ('Hi {{1}}, {{2}} ... {{1}}', ['name', 'date'])."""
    order: list[str] = []

    def number(match: re.Match) -> str:
        key = match.group(1).lower()
        if key not in order:
            order.append(key)
        return "{{" + str(order.index(key) + 1) + "}}"

    return _NAMED.sub(number, body), order


def validate(t: WhatsAppTemplate) -> list[str]:
    """Meta's rules, in words a business owner understands."""
    problems = []
    if not re.fullmatch(r"[a-z0-9_]{1,512}", t.name or ""):
        problems.append("The template name can only use lowercase letters, numbers and underscores.")
    if t.category not in CATEGORIES:
        problems.append("Choose a type: booking/updates, marketing, or login code.")
    body = (t.body or "").strip()
    if not body:
        problems.append("Write the message.")
    if len(body) > 1024:
        problems.append("The message is too long (Meta allows 1,024 characters).")
    if _NUMBERED.search(body):
        problems.append("Use named blanks like {{name}} instead of numbers like {{1}}.")
    if re.match(r"^\s*\{\{", body) or re.search(r"\}\}\s*[.!?]?\s*$", body):
        problems.append("Meta doesn't allow a blank like {{name}} at the very start or end. Add a word before or after it.")
    if re.search(r"\}\}\s*\{\{", body):
        problems.append("Put at least one word between two blanks, e.g. {{date}} at {{time}}.")
    _, keys = compile_body(body)
    if keys and len(re.sub(_NAMED, "", body).split()) < 2 * len(keys):
        problems.append("Add more words around the blanks: Meta rejects templates that are mostly blanks.")
    if t.header_text and (len(t.header_text) > 60 or "{{" in t.header_text):
        problems.append("The title must be under 60 characters, without blanks.")
    if t.footer_text and len(t.footer_text) > 60:
        problems.append("The small print must be under 60 characters.")
    buttons = t.buttons or []
    if len(buttons) > 10:
        problems.append("Use at most 10 buttons.")
    for b in buttons:
        if not (b.get("text") or "").strip() or len(b["text"]) > 25:
            problems.append("Each button needs a label of up to 25 characters.")
        if b.get("type") == "URL" and not str(b.get("url") or "").startswith("https://"):
            problems.append("A website button needs a link starting with https://.")
        if b.get("type") == "PHONE_NUMBER" and not re.fullmatch(r"\+\d{8,15}", str(b.get("phone_number") or "")):
            problems.append("A call button needs a phone number like +919876543210.")
    if t.category == "AUTHENTICATION" and keys != ["code"]:
        problems.append("A login-code template must contain exactly one blank: {{code}}.")
    return problems


def example_value(t: WhatsAppTemplate, key: str) -> str:
    return str((t.examples or {}).get(key) or VARIABLES.get(key, {}).get("example") or key.replace("_", " "))


def meta_payload(t: WhatsAppTemplate) -> dict:
    text, keys = compile_body(t.body)
    body: dict = {"type": "BODY", "text": text}
    if keys:
        body["example"] = {"body_text": [[example_value(t, k) for k in keys]]}
    components: list[dict] = []
    if t.header_text:
        components.append({"type": "HEADER", "format": "TEXT", "text": t.header_text})
    components.append(body)
    if t.footer_text:
        components.append({"type": "FOOTER", "text": t.footer_text})
    if t.buttons:
        buttons = []
        for b in t.buttons:
            if b.get("type") == "URL":
                buttons.append({"type": "URL", "text": b["text"], "url": b["url"]})
            elif b.get("type") == "PHONE_NUMBER":
                buttons.append({"type": "PHONE_NUMBER", "text": b["text"], "phone_number": b["phone_number"]})
            else:
                buttons.append({"type": "QUICK_REPLY", "text": b["text"]})
        components.append({"type": "BUTTONS", "buttons": buttons})
    return {"name": t.name, "language": t.language, "category": t.category, "components": components}


def render(t: WhatsAppTemplate, values: dict[str, str]) -> str:
    """What the customer will read, for previews and the inbox history."""
    text = _NAMED.sub(lambda m: str(values.get(m.group(1).lower()) or example_value(t, m.group(1).lower())), t.body)
    return "\n".join(p for p in (t.header_text, text, t.footer_text) if p)


def body_parameters(t: WhatsAppTemplate, values: dict[str, str]) -> list[dict]:
    keys = t.variables or compile_body(t.body)[1]
    return [{"type": "text", "text": (str(values.get(k) or "").strip() or "-")[:1000]} for k in keys]


# ------------------------------------------------------------------ Meta Graph API


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{get_settings().meta_graph_api_version}/{path.lstrip('/')}"


def _headers(account: WhatsAppAccount) -> dict:
    return {"Authorization": f"Bearer {decrypt_secret(account.encrypted_access_token)}"}


def _meta_error(response: httpx.Response) -> str:
    try:
        err = response.json().get("error") or {}
    except ValueError:
        return response.text[:300]
    detail = err.get("error_user_msg") or err.get("message") or response.text[:300]
    return friendly_error(err.get("code"), detail)


async def account_for(db: AsyncSession, t: WhatsAppTemplate) -> WhatsAppAccount:
    query = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == t.tenant_id)
    if t.whatsapp_account_id:
        query = query.where(WhatsAppAccount.id == t.whatsapp_account_id)
    account = (await db.execute(query.limit(1))).scalar_one_or_none()
    if account is None:
        raise TemplateError("Connect a WhatsApp number first.")
    return account


async def submit(db: AsyncSession, t: WhatsAppTemplate, client: httpx.AsyncClient | None = None) -> WhatsAppTemplate:
    problems = validate(t)
    if problems:
        raise TemplateError(" ".join(problems))
    account = await account_for(db, t)
    t.whatsapp_account_id = account.id
    t.variables = compile_body(t.body)[1]
    http = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await http.post(_graph(f"{account.waba_id}/message_templates"), headers=_headers(account), json=meta_payload(t))
    finally:
        if client is None:
            await http.aclose()
    if response.status_code >= 400:
        raise TemplateError(_meta_error(response))
    data = response.json()
    t.meta_template_id = str(data.get("id") or "") or None
    t.status = str(data.get("status") or "PENDING").upper()
    t.category = str(data.get("category") or t.category).upper()  # Meta may re-categorise
    t.rejection_reason = None
    return t


async def delete_at_meta(db: AsyncSession, t: WhatsAppTemplate, client: httpx.AsyncClient | None = None) -> None:
    if not t.meta_template_id:
        return
    account = await account_for(db, t)
    http = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await http.delete(
            _graph(f"{account.waba_id}/message_templates"),
            headers=_headers(account),
            params={"name": t.name, "hsm_id": t.meta_template_id},
        )
    finally:
        if client is None:
            await http.aclose()
    if response.status_code >= 400 and response.status_code != 404:
        raise TemplateError(_meta_error(response))


def _from_meta_components(components: list[dict]) -> dict:
    out: dict = {"header_text": None, "body": "", "footer_text": None, "buttons": []}
    for c in components or []:
        kind = (c.get("type") or "").upper()
        if kind == "HEADER" and (c.get("format") or "TEXT").upper() == "TEXT":
            out["header_text"] = (c.get("text") or "")[:60] or None
        elif kind == "BODY":
            # Templates made directly in Meta use numbers; name them var1, var2...
            out["body"] = _NUMBERED.sub(lambda m: "{{var" + m.group(1) + "}}", c.get("text") or "")
        elif kind == "FOOTER":
            out["footer_text"] = (c.get("text") or "")[:60] or None
        elif kind == "BUTTONS":
            out["buttons"] = [
                {k: v for k, v in b.items() if k in ("type", "text", "url", "phone_number")}
                for b in c.get("buttons") or []
            ]
    return out


async def sync_from_meta(db: AsyncSession, tenant_id: uuid.UUID, client: httpx.AsyncClient | None = None) -> int:
    """Pull approval status (and templates made directly in Meta) for every number."""
    accounts = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))).scalars().all()
    existing = {
        (t.name, t.language): t
        for t in (await db.execute(select(WhatsAppTemplate).where(WhatsAppTemplate.tenant_id == tenant_id))).scalars().all()
    }
    changed = 0
    http = client or httpx.AsyncClient(timeout=20.0)
    try:
        for account in {a.waba_id: a for a in accounts}.values():
            response = await http.get(
                _graph(f"{account.waba_id}/message_templates"),
                headers=_headers(account),
                params={"fields": "id,name,language,status,category,rejected_reason,components", "limit": 200},
            )
            if response.status_code >= 400:
                logger.warning("template sync for WABA %s failed: %s", account.waba_id, _meta_error(response))
                continue
            for item in response.json().get("data") or []:
                key = (item.get("name"), item.get("language"))
                t = existing.get(key)
                if t is None:
                    parts = _from_meta_components(item.get("components") or [])
                    t = WhatsAppTemplate(
                        tenant_id=tenant_id,
                        whatsapp_account_id=account.id,
                        name=item["name"],
                        language=item.get("language") or "en",
                        category=(item.get("category") or "UTILITY").upper(),
                        purpose="custom",
                        variables=compile_body(parts["body"])[1],
                        examples={},
                        **parts,
                    )
                    db.add(t)
                    existing[key] = t
                    changed += 1
                status = (item.get("status") or "").upper()
                reason = item.get("rejected_reason")
                if status and (t.status != status or t.meta_template_id != item.get("id")):
                    t.status = status
                    t.meta_template_id = item.get("id")
                    t.rejection_reason = reason if reason and reason != "NONE" else None
                    changed += 1
    finally:
        if client is None:
            await http.aclose()
    return changed


async def apply_status_update(db: AsyncSession, value: dict) -> bool:
    """Webhook field message_template_status_update: APPROVED / REJECTED / PAUSED..."""
    template_id = str(value.get("message_template_id") or "")
    if not template_id:
        return False
    t = (await db.execute(select(WhatsAppTemplate).where(WhatsAppTemplate.meta_template_id == template_id))).scalars().first()
    if t is None:
        return False
    t.status = str(value.get("event") or t.status).upper()[:16]
    reason = value.get("reason")
    t.rejection_reason = reason if reason and reason != "NONE" else None
    return True


# ------------------------------------------------------------------ sending


async def find_approved(
    db: AsyncSession, tenant_id: uuid.UUID, *, purpose: str | None = None, name: str | None = None
) -> WhatsAppTemplate | None:
    query = select(WhatsAppTemplate).where(WhatsAppTemplate.tenant_id == tenant_id, WhatsAppTemplate.status == "APPROVED")
    if purpose:
        query = query.where(WhatsAppTemplate.purpose == purpose)
    if name:
        query = query.where(WhatsAppTemplate.name == name)
    return (await db.execute(query.order_by(WhatsAppTemplate.updated_at.desc()).limit(1))).scalar_one_or_none()


async def context_values(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    customer: Customer | None,
    *,
    appointment=None,
    extra: dict | None = None,
) -> dict[str, str]:
    """Fill blanks from what we know: customer, business, a booking, plus extras."""
    from app.services.customer_context import spoken_time

    from packages.database.models.operations import AppointmentResource, BookableService

    values: dict[str, str] = {}
    tenant = await db.get(Tenant, tenant_id)
    if tenant:
        values["business"] = tenant.name
    if customer and customer.name and customer.name != customer.phone:
        values["name"] = customer.name.split()[0]
    else:
        values["name"] = "there"
    if appointment is not None:
        service = await db.get(BookableService, appointment.service_id)
        resource = await db.get(AppointmentResource, appointment.resource_id)
        tz = resource.timezone if resource else "UTC"
        when = spoken_time(appointment.start_at, tz)  # "Saturday 4 October, 2:00 pm"
        day, _, clock = when.partition(", ")
        values |= {"service": service.name if service else "", "date": day, "time": clock, "staff": resource.name if resource else ""}
        values["order_id"] = str(appointment.id)[:8].upper()
    return values | {k: str(v) for k, v in (extra or {}).items() if v is not None}


async def send_template(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    to: str,
    template: WhatsAppTemplate,
    values: dict[str, str],
    account: WhatsAppAccount | None = None,
    metered: bool = True,
) -> str | None:
    """Sends an approved template with its blanks filled. Returns Meta's message id.
    `metered=False` when the caller saves the Message itself (it is counted from that)."""
    if template.status != "APPROVED":
        raise TemplateError(f"The template '{template.name}' isn't approved by Meta yet.")
    account = account or await account_for(db, template)
    components = []
    params = body_parameters(template, values)
    if params:
        components.append({"type": "body", "parameters": params})
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": re.sub(r"\D", "", to),
        "type": "template",
        "template": {"name": template.name, "language": {"code": template.language}, "components": components},
    }
    from app.integrations.whatsapp import _claim_or_stop, _release_claim

    claim_id = await _claim_or_stop(tenant_id, metered)
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                _graph(f"{account.phone_number_id}/messages"), headers=_headers(account), json=payload
            )
        if response.status_code >= 400:
            raise WhatsAppAPIError(_meta_error(response))
    except Exception:
        await _release_claim(claim_id)
        raise
    return ((response.json().get("messages") or [{}])[0]).get("id")


def window_open(last_customer_message_at: datetime | None, now: datetime | None = None) -> bool:
    """WhatsApp's 24-hour customer-service window: free text is allowed inside it."""
    if last_customer_message_at is None:
        return False
    return (now or datetime.now(UTC)) - last_customer_message_at < timedelta(hours=24)


async def notify_booking(db: AsyncSession, tenant_id: uuid.UUID, appointment, purpose: str) -> bool:
    """Booked / moved / cancelled: send the business's approved template for it,
    if they have one. Best effort: a WhatsApp problem never undoes the booking."""
    template = await find_approved(db, tenant_id, purpose=purpose)
    if template is None:
        return False
    customer = await db.get(Customer, appointment.customer_id)
    if customer is None or not customer.phone:
        return False
    try:
        values = await context_values(db, tenant_id, customer, appointment=appointment)
        await send_template(db, tenant_id, to=customer.phone, template=template, values=values)
    except (TemplateError, WhatsAppAPIError, httpx.HTTPError, PlanLimitError) as exc:
        logger.warning("booking %s: %s template not sent: %s", appointment.id, purpose, exc)
        return False
    return True
