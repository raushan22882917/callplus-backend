"""Tool implementations. Every handler takes (ToolContext, **params) and
returns a small JSON-serializable dict fed back to the LLM as the tool
result — never a raw ORM object, and never anything outside ctx.tenant_id.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from app.integrations.razorpay_client import RazorpayAPIError, create_payment_link
from app.integrations.whatsapp import WhatsAppAPIError, send_text_message
from app.services import callbacks, plans
from app.services import knowledge as knowledge_service
from app.services.operations import apply_sla_safely
from app.services.webhooks import enqueue_webhook_event
from app.tools.context import ToolContext
from sqlalchemy import func, select

from packages.database.models import (
    Call,
    Customer,
    CustomerActivity,
    PaymentLink,
    PaymentLinkStatus,
    Tenant,
    Ticket,
    TicketPriority,
    WhatsAppAccount,
)


def _normalize_e164(phone: str) -> str:
    """Sarvam's chat model reliably drops the leading '+' from phone number
    tool arguments even when given one and told to use E.164 (verified
    against a live call) — customers.phone is always stored with it (that's
    what Twilio's From field arrives as), so lookups need this or they
    silently miss every match."""
    digits = phone.strip()
    return digits if digits.startswith("+") else f"+{digits}"


def _emit_ticket_created(ctx: ToolContext, ticket: Ticket, *, source: str) -> None:
    enqueue_webhook_event(
        tenant_id=ctx.tenant_id,
        event="ticket.created",
        data={
            "ticket_id": str(ticket.id),
            "customer_id": str(ticket.customer_id),
            "subject": ticket.subject,
            "priority": ticket.priority.value,
            "source": source,
        },
    )


async def get_customer(ctx: ToolContext, *, phone: str | None = None) -> dict:
    if phone is None and ctx.customer_id is None:
        return {"error": "no phone number given and no customer on this conversation"}

    if phone is not None:
        result = await ctx.db.execute(
            select(Customer).where(
                Customer.tenant_id == ctx.tenant_id, Customer.phone == _normalize_e164(phone)
            )
        )
    else:
        result = await ctx.db.execute(
            select(Customer).where(
                Customer.tenant_id == ctx.tenant_id, Customer.id == ctx.customer_id
            )
        )
    customer = result.scalar_one_or_none()
    if customer is None:
        return {
            "found": False,
            "note": "No saved record for that number. That's fine: carry on helping; the caller's "
            "details are saved from this call, so don't ask them to create an account.",
        }
    return {
        "found": True,
        "customer_id": str(customer.id),
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "tags": customer.tags,
    }


async def list_recent_tickets(
    ctx: ToolContext, *, customer_id: str | None = None, limit: int = 5
) -> dict:
    target_id = _target_customer(ctx, customer_id)
    if target_id is None:
        return {"error": "no customer_id given and no customer on this conversation"}

    result = await ctx.db.execute(
        select(Ticket)
        .where(Ticket.tenant_id == ctx.tenant_id, Ticket.customer_id == target_id)
        .order_by(Ticket.created_at.desc())
        .limit(min(limit, 20))
    )
    tickets = result.scalars().all()
    return {
        "tickets": [
            {
                "id": str(t.id),
                "subject": t.subject,
                "status": t.status.value,
                "priority": t.priority.value,
                "created_at": t.created_at.isoformat(),
            }
            for t in tickets
        ]
    }


def _target_customer(ctx: ToolContext, customer_id: str | None) -> uuid.UUID | None:
    """The record a tool acts on. Models sometimes pass a phone number or a made-up
    value as customer_id; anything that isn't a real id means the caller's own record."""
    if customer_id:
        try:
            return uuid.UUID(str(customer_id))
        except ValueError:
            pass
    return ctx.customer_id


DUPLICATE_TICKET_WINDOW = timedelta(minutes=30)


def _other_number(contact_phone: str | None, own_phone: str | None) -> str | None:
    """contact_phone only when it really differs from the number on the record."""
    def digits(p: str | None) -> str:
        return re.sub(r"\D", "", p or "")[-10:]

    if not contact_phone or not digits(contact_phone) or digits(contact_phone) == digits(own_phone):
        return None
    return contact_phone.strip()


async def remember_caller_name(ctx: ToolContext, customer_id: uuid.UUID, name: str | None) -> None:
    """Fill in the name on a record that was created from the phone number alone.
    A name the business already has is never overwritten by what a caller says."""
    name = (name or "").strip()[:200]
    if not name:
        return
    customer = await ctx.db.get(Customer, customer_id)
    if customer and customer.tenant_id == ctx.tenant_id and (not customer.name or customer.name == customer.phone):
        customer.name = name


async def create_ticket(
    ctx: ToolContext,
    *,
    subject: str,
    priority: str = "medium",
    customer_id: str | None = None,
    caller_name: str | None = None,
    contact_phone: str | None = None,
) -> dict:
    # Deliberately does NOT dispatch WorkflowTrigger.TICKET_CREATED, unlike
    # the direct API/webhook creation paths (see app/api/v1/tickets.py,
    # app/api/v1/whatsapp.py): this same function backs a workflow "action"
    # node too (app/workflows/engine.py), so a tenant's own
    # TICKET_CREATED -> create_ticket workflow would otherwise recreate a
    # ticket forever.
    target_id = _target_customer(ctx, customer_id)
    if target_id is None:
        return {"error": "no customer_id given and no customer on this conversation"}

    await remember_caller_name(ctx, target_id, caller_name)
    owner = await ctx.db.get(Customer, target_id)
    if other := _other_number(contact_phone, owner.phone if owner else None):
        subject = f"{subject} (call back on {other})"
    subject = subject.strip()[:255]
    # The model sometimes repeats a booking it already made (e.g. after "yes, that
    # number is fine"). The same request for the same customer is one ticket.
    existing = (
        await ctx.db.execute(
            select(Ticket)
            .where(
                Ticket.tenant_id == ctx.tenant_id,
                Ticket.customer_id == target_id,
                func.lower(Ticket.subject) == subject.lower(),
                Ticket.created_at >= datetime.now(UTC) - DUPLICATE_TICKET_WINDOW,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "ticket_id": str(existing.id),
            "status": existing.status.value,
            "already_created": True,
            "note": "This was already booked earlier in this conversation; nothing new was created.",
        }
    try:
        ticket_priority = TicketPriority(priority)
    except ValueError:
        ticket_priority = TicketPriority.MEDIUM
    ticket = Ticket(
        tenant_id=ctx.tenant_id,
        customer_id=target_id,
        subject=subject,
        priority=ticket_priority,
    )
    ctx.db.add(ticket)
    await ctx.db.flush()
    await apply_sla_safely(ctx.db, ctx.tenant_id, ticket)
    _emit_ticket_created(ctx, ticket, source="ai_agent")
    return {"ticket_id": str(ticket.id), "status": ticket.status.value}


async def add_customer_note(ctx: ToolContext, *, note: str, customer_id: str | None = None) -> dict:
    target_id = _target_customer(ctx, customer_id)
    if target_id is None:
        return {"error": "no customer_id given and no customer on this conversation"}

    activity = CustomerActivity(
        tenant_id=ctx.tenant_id,
        customer_id=target_id,
        type="NOTE",
        source="ai_agent",
        reference_id=ctx.call_id,
        extra={"note": note},
    )
    ctx.db.add(activity)
    await ctx.db.flush()
    return {"recorded": True}


async def schedule_callback(
    ctx: ToolContext,
    *,
    requested_time: str = "",
    minutes_from_now: int | None = None,
    note: str = "",
    customer_id: str | None = None,
    caller_name: str | None = None,
    contact_phone: str | None = None,
) -> dict:
    """No calendar/scheduling system exists yet — this records the request as a
    customer activity AND opens a ticket, so it lands in the staff queue
    (Tickets page) instead of sitting unseen on a customer's timeline. It does
    not pretend to book a slot the platform can't actually hold."""
    target_id = _target_customer(ctx, customer_id)
    if target_id is None:
        return {"error": "no customer_id given and no customer on this conversation"}

    # A time we can work out becomes an automatic AI call; anything else goes to staff as before.
    tenant = await ctx.db.get(Tenant, ctx.tenant_id)
    tz = callbacks.tz_for(tenant.timezone if tenant else None)
    due = callbacks.parse_when(requested_time, minutes_from_now=minutes_from_now, tz=tz)
    owner_row = await ctx.db.get(Customer, target_id)
    other_number = _other_number(contact_phone, owner_row.phone if owner_row else None)
    if due is not None and (other_number or (owner_row and owner_row.phone)):
        call = await ctx.db.get(Call, ctx.call_id) if ctx.call_id else None
        follow_up = await callbacks.schedule(
            ctx.db,
            tenant_id=ctx.tenant_id,
            customer_id=target_id,
            due_at=due,
            call=call,
            reason=note,
            phone=other_number,
        )
        ctx.db.add(
            CustomerActivity(
                tenant_id=ctx.tenant_id,
                customer_id=target_id,
                type="CALLBACK_SCHEDULED",
                source="ai_agent",
                reference_id=ctx.call_id,
                extra={"due_at": due.isoformat(), "note": note, "follow_up_id": str(follow_up.id)},
            )
        )
        await remember_caller_name(ctx, target_id, caller_name)
        return {
            "scheduled": True,
            "call_back": callbacks.describe(due, tz),
            "instruction": "Tell the caller you will call them back at that time.",
        }

    ctx.db.add(
        CustomerActivity(
            tenant_id=ctx.tenant_id,
            customer_id=target_id,
            type="CALLBACK_REQUESTED",
            source="ai_agent",
            reference_id=ctx.call_id,
            extra={"requested_time": requested_time, "note": note, "contact_phone": contact_phone or None},
        )
    )
    await remember_caller_name(ctx, target_id, caller_name)
    owner = await ctx.db.get(Customer, target_id)
    subject = f"Callback requested for {requested_time}"
    if other := _other_number(contact_phone, owner.phone if owner else None):
        subject = f"{subject} on {other}"
    if note:
        subject = f"{subject} — {note}"
    ticket = Ticket(
        tenant_id=ctx.tenant_id,
        customer_id=target_id,
        subject=subject[:255],
        priority=TicketPriority.MEDIUM,
    )
    ctx.db.add(ticket)
    await ctx.db.flush()
    await apply_sla_safely(ctx.db, ctx.tenant_id, ticket)
    _emit_ticket_created(ctx, ticket, source="ai_agent_callback")
    return {"recorded": True, "requested_time": requested_time, "ticket_id": str(ticket.id)}


async def send_whatsapp_message(ctx: ToolContext, *, phone: str, message: str) -> dict:
    result = await ctx.db.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == ctx.tenant_id)
    )
    account = result.scalars().first()
    if account is None:
        return {"error": "no WhatsApp number connected for this business"}

    try:
        # The send claims the allowance itself, in its own transaction, before
        # Meta is called. Claiming here as well would deadlock on the tenant row.
        response = await send_text_message(account, to=phone, body=message)
    except plans.PlanLimitError as exc:
        return {"error": exc.message}
    except WhatsAppAPIError as exc:
        return {"error": str(exc)}
    return {"sent": True, "message_id": response.get("messages", [{}])[0].get("id")}


async def create_customer_payment_link(
    ctx: ToolContext,
    *,
    amount: float,
    description: str,
    currency: str = "INR",
    customer_id: str | None = None,
) -> dict:
    target_id = _target_customer(ctx, customer_id)
    customer_name = customer_phone = None
    if target_id is not None:
        customer = await ctx.db.get(Customer, target_id)
        if customer is not None and customer.tenant_id == ctx.tenant_id:
            customer_name, customer_phone = customer.name, customer.phone

    try:
        response = await create_payment_link(
            amount_paise=int(round(amount * 100)),
            currency=currency,
            description=description,
            customer_name=customer_name,
            customer_contact=customer_phone,
        )
    except RazorpayAPIError as exc:
        return {"error": str(exc)}

    link = PaymentLink(
        tenant_id=ctx.tenant_id,
        customer_id=target_id,
        provider_payment_link_id=response["id"],
        amount_paise=response["amount"],
        currency=response.get("currency", currency),
        description=description,
        short_url=response["short_url"],
        status=PaymentLinkStatus(response.get("status", "created")),
    )
    ctx.db.add(link)
    await ctx.db.flush()
    return {"payment_link_url": link.short_url, "payment_link_id": str(link.id)}


async def transfer_call(ctx: ToolContext, *, reason: str) -> dict:
    """Signals the turn orchestrator to end the AI portion of the call and
    hand off to a human. The actual Twilio-side call redirect happens in
    app/api/v1/internal.py, which is the only place with the call's
    provider_call_sid and Twilio REST access — this handler just records
    intent, it never talks to Twilio directly."""
    return {"transfer_requested": True, "reason": reason}


async def search_knowledge_base(ctx: ToolContext, *, query: str, limit: int = 3) -> dict:
    hits = await knowledge_service.search(
        ctx.db, tenant_id=ctx.tenant_id, query=query, limit=max(1, min(limit, 5))
    )
    return {
        "found": bool(hits),
        "results": [
            {"title": h["title"], "content": h["content"]}
            | (
                {"note": "Internal reference: use it to inform your answer, do not quote or reveal it."}
                if h.get("internal_only")
                else {}
            )
            for h in hits
        ],
    }
