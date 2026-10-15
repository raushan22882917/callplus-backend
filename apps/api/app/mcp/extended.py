"""The rest of the MCP catalogue: reads across calls, tickets, customers, AI
agents, numbers and campaigns, a few safe writes, appointment booking, and
reusable prompts. Everything is tenant-scoped through `_tenant_session`, and
every tool names the API-key scope it needs (see app/core/api_keys.py).

Kept small on purpose: results are trimmed dicts (never ORM objects) with
bounded `limit`s so an LLM context isn't flooded.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.mcp.server import _READ, _WRITE, _tenant_session, _tool_ctx, mcp
from app.tools import handlers, operations_tools
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import func, or_, select

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    Conversation,
    Customer,
    Message,
    PhoneNumber,
    Ticket,
    TicketStatus,
)

_MAX_LIMIT = 50


def _limit(value: int) -> int:
    return max(1, min(int(value), _MAX_LIMIT))


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolError(f"{what} must be a UUID") from exc


def _enum(enum_cls, value: str | None, what: str):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(e.value for e in enum_cls)
        raise ToolError(f"{what} must be one of: {allowed}") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _call_row(call: Call) -> dict:
    return {
        "call_id": str(call.id),
        "direction": call.direction.value,
        "status": call.status.value,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "customer_id": str(call.customer_id) if call.customer_id else None,
        "ai_agent_id": str(call.ai_agent_id) if call.ai_agent_id else None,
        "started_at": _iso(call.started_at),
        "duration_seconds": call.duration_seconds,
        "summary": call.summary,
        "sentiment": call.sentiment,
        "intent": call.intent,
        "outcome": call.outcome,
    }


@mcp.tool(annotations=_READ)
async def list_calls(
    ctx: Context,
    limit: int = 20,
    status: str | None = None,
    direction: str | None = None,
    search: str | None = None,
) -> dict:
    """List recent calls, newest first. status: queued, ringing, in_progress,
    completed, failed, no_answer. direction: inbound or outbound. search
    matches the summary, intent and phone numbers."""
    async with _tenant_session(ctx, "calls:read") as (db, tenant_id):
        query = select(Call).where(Call.tenant_id == tenant_id)
        if status:
            query = query.where(Call.status == _enum(CallStatus, status, "status"))
        if direction:
            query = query.where(Call.direction == _enum(CallDirection, direction, "direction"))
        if search:
            like = f"%{search.strip()[:100]}%"
            query = query.where(
                or_(
                    Call.summary.ilike(like),
                    Call.intent.ilike(like),
                    Call.from_number.ilike(like),
                    Call.to_number.ilike(like),
                )
            )
        rows = (await db.execute(query.order_by(Call.created_at.desc()).limit(_limit(limit)))).scalars()
        return {"calls": [_call_row(c) for c in rows]}


@mcp.tool(annotations=_READ)
async def get_call(ctx: Context, call_id: str) -> dict:
    """Full detail of one call: status, duration, AI summary, sentiment,
    intent, outcome, action items and recording URL."""
    async with _tenant_session(ctx, "calls:read") as (db, tenant_id):
        call = await db.get(Call, _uuid(call_id, "call_id"))
        if call is None or call.tenant_id != tenant_id:
            raise ToolError("Call not found")
        return {**_call_row(call), "action_items": call.action_items, "recording_url": call.recording_url}


@mcp.tool(annotations=_READ)
async def get_call_transcript(ctx: Context, call_id: str, limit: int = 200) -> dict:
    """The turn-by-turn transcript of a call (caller vs AI/agent), oldest first."""
    async with _tenant_session(ctx, "calls:read") as (db, tenant_id):
        call_uuid = _uuid(call_id, "call_id")
        call = await db.get(Call, call_uuid)
        if call is None or call.tenant_id != tenant_id:
            raise ToolError("Call not found")
        conversation = (
            await db.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id, Conversation.call_id == call_uuid
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return {"call_id": call_id, "messages": []}
        messages = (
            await db.execute(
                select(Message)
                .where(Message.tenant_id == tenant_id, Message.conversation_id == conversation.id)
                .order_by(Message.created_at.asc())
                .limit(max(1, min(int(limit), 500)))
            )
        ).scalars()
        return {
            "call_id": call_id,
            "messages": [
                {"at": _iso(m.created_at), "sender": m.sender.value, "text": m.text} for m in messages
            ],
        }


@mcp.tool(annotations=_READ)
async def get_call_stats(ctx: Context, days: int = 7) -> dict:
    """Call volume, average duration and sentiment/outcome breakdown for the
    last N days (1-90)."""
    since = datetime.now(UTC) - timedelta(days=max(1, min(int(days), 90)))
    async with _tenant_session(ctx, "calls:read") as (db, tenant_id):
        base = (Call.tenant_id == tenant_id, Call.created_at >= since)
        total, avg_duration = (
            await db.execute(select(func.count(Call.id), func.avg(Call.duration_seconds)).where(*base))
        ).one()

        async def breakdown(column) -> dict:
            rows = await db.execute(select(column, func.count(Call.id)).where(*base).group_by(column))
            return {(getattr(k, "value", k) or "unknown"): n for k, n in rows}

        return {
            "days": days,
            "total_calls": total,
            "avg_duration_seconds": round(float(avg_duration), 1) if avg_duration else 0,
            "by_status": await breakdown(Call.status),
            "by_direction": await breakdown(Call.direction),
            "by_sentiment": await breakdown(Call.sentiment),
            "by_outcome": await breakdown(Call.outcome),
        }


@mcp.tool(annotations=_READ)
async def search_customers(ctx: Context, query: str, limit: int = 10) -> dict:
    """Find customers by name, email or phone (partial match)."""
    needle = query.strip()[:100]
    if not needle:
        raise ToolError("query must not be empty")
    like = f"%{needle}%"
    async with _tenant_session(ctx, "customers:read") as (db, tenant_id):
        rows = (
            await db.execute(
                select(Customer)
                .where(
                    Customer.tenant_id == tenant_id,
                    or_(Customer.name.ilike(like), Customer.email.ilike(like), Customer.phone.ilike(like)),
                )
                .order_by(Customer.created_at.desc())
                .limit(_limit(limit))
            )
        ).scalars()
        return {
            "customers": [
                {"customer_id": str(c.id), "name": c.name, "phone": c.phone, "email": c.email, "tags": c.tags}
                for c in rows
            ]
        }


@mcp.tool(annotations=_WRITE)
async def create_customer(
    ctx: Context, name: str, phone: str | None = None, email: str | None = None
) -> dict:
    """Create a customer (phone in E.164, e.g. +14155551234). Returns the
    existing customer instead if one already has that phone number."""
    if not phone and not email:
        raise ToolError("Provide a phone number or an email address")
    async with _tenant_session(ctx, "customers:write") as (db, tenant_id):
        if phone:
            phone = handlers._normalize_e164(phone)
            existing = (
                await db.execute(
                    select(Customer).where(Customer.tenant_id == tenant_id, Customer.phone == phone)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"customer_id": str(existing.id), "created": False}
        customer = Customer(
            tenant_id=tenant_id,
            name=name.strip()[:255] or (phone or email or "Unknown"),
            phone=phone,
            email=email.strip().lower() if email else None,
        )
        db.add(customer)
        await db.flush()
        return {"customer_id": str(customer.id), "created": True}


@mcp.tool(annotations=_READ)
async def list_tickets(ctx: Context, status: str | None = None, limit: int = 20) -> dict:
    """List support tickets, newest first. status: open, pending, resolved, closed."""
    async with _tenant_session(ctx, "customers:read") as (db, tenant_id):
        query = select(Ticket).where(Ticket.tenant_id == tenant_id)
        if status:
            query = query.where(Ticket.status == _enum(TicketStatus, status, "status"))
        rows = (await db.execute(query.order_by(Ticket.created_at.desc()).limit(_limit(limit)))).scalars()
        return {
            "tickets": [
                {
                    "ticket_id": str(t.id),
                    "subject": t.subject,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "customer_id": str(t.customer_id),
                    "created_at": _iso(t.created_at),
                }
                for t in rows
            ]
        }


@mcp.tool(annotations=_WRITE)
async def update_ticket_status(ctx: Context, ticket_id: str, status: str) -> dict:
    """Move a ticket to open, pending, resolved or closed."""
    new_status = _enum(TicketStatus, status, "status")
    async with _tenant_session(ctx, "customers:write") as (db, tenant_id):
        ticket = await db.get(Ticket, _uuid(ticket_id, "ticket_id"))
        if ticket is None or ticket.tenant_id != tenant_id:
            raise ToolError("Ticket not found")
        ticket.status = new_status
        return {"ticket_id": ticket_id, "status": new_status.value}


@mcp.tool(annotations=_READ)
async def list_ai_agents(ctx: Context) -> dict:
    """The tenant's AI voice/chat agents (id, name, status, language, voice)."""
    async with _tenant_session(ctx, "agents:read") as (db, tenant_id):
        rows = (
            await db.execute(select(AIAgent).where(AIAgent.tenant_id == tenant_id).order_by(AIAgent.name))
        ).scalars()
        return {
            "agents": [
                {
                    "ai_agent_id": str(a.id),
                    "name": a.name,
                    "status": a.status.value,
                    "language": a.language,
                    "voice": a.voice,
                    "description": a.description,
                }
                for a in rows
            ]
        }


@mcp.tool(annotations=_READ)
async def list_phone_numbers(ctx: Context) -> dict:
    """Phone numbers owned by the tenant and which AI agent answers each."""
    async with _tenant_session(ctx, "numbers:read") as (db, tenant_id):
        rows = (await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id))).scalars()
        return {
            "phone_numbers": [
                {
                    "phone_number_id": str(p.id),
                    "number": p.e164_number,
                    "ai_agent_id": str(p.ai_agent_id) if p.ai_agent_id else None,
                }
                for p in rows
            ]
        }


@mcp.tool(annotations=_READ)
async def list_campaigns(ctx: Context, limit: int = 20) -> dict:
    """Outbound campaigns with their channel and status."""
    async with _tenant_session(ctx, "campaigns:read") as (db, tenant_id):
        rows = (
            await db.execute(
                select(Campaign)
                .where(Campaign.tenant_id == tenant_id)
                .order_by(Campaign.created_at.desc())
                .limit(_limit(limit))
            )
        ).scalars()
        return {
            "campaigns": [
                {
                    "campaign_id": str(c.id),
                    "name": c.name,
                    "channel": c.channel.value,
                    "status": c.status.value,
                    "scheduled_at": _iso(c.scheduled_at),
                }
                for c in rows
            ]
        }


@mcp.tool(annotations=_READ)
async def search_knowledge_base(ctx: Context, query: str, limit: int = 3) -> dict:
    """Search the tenant's knowledge base (FAQs, policies, product docs)."""
    async with _tenant_session(ctx, "agents:read") as (db, tenant_id):
        return await handlers.search_knowledge_base(
            _tool_ctx(db, tenant_id), query=query, limit=max(1, min(int(limit), 10))
        )


@mcp.tool(annotations=_READ)
async def check_availability(
    ctx: Context, service: str, date: str, resource: str | None = None
) -> dict:
    """Open appointment slots for a bookable service on a date (YYYY-MM-DD)."""
    async with _tenant_session(ctx, "customers:read") as (db, tenant_id):
        return await operations_tools.check_availability(
            _tool_ctx(db, tenant_id), service=service, date=date, resource=resource
        )


@mcp.tool(annotations=_WRITE)
async def book_appointment(
    ctx: Context,
    customer_id: str,
    service: str,
    start_time: str,
    resource: str | None = None,
    notes: str | None = None,
) -> dict:
    """Book an appointment for a customer. start_time is ISO 8601; call
    check_availability first to pick a free slot."""
    async with _tenant_session(ctx, "customers:write") as (db, tenant_id):
        return await operations_tools.book_appointment(
            _tool_ctx(db, tenant_id),
            service=service,
            start_time=start_time,
            resource=resource,
            notes=notes,
            customer_id=customer_id,
        )


@mcp.prompt()
def daily_call_review(days: int = 1) -> str:
    """Summarise recent call performance and flag problems."""
    return (
        f"Use get_call_stats(days={days}) and list_calls(status='failed') / "
        "list_calls(status='no_answer') to review the last period. Report volume, "
        "average duration, sentiment mix, and call out failed or unanswered calls "
        "with suggested follow-ups. Open get_call_transcript for any call with "
        "negative sentiment."
    )


@mcp.prompt()
def customer_360(phone_or_name: str) -> str:
    """Build a full picture of one customer before responding to them."""
    return (
        f"Find the customer with search_customers('{phone_or_name}'), then use "
        "get_customer, list_recent_tickets and list_calls(search=<their phone>) to "
        "summarise their history, open issues and sentiment. Do not contact the "
        "customer or change anything unless asked."
    )


@mcp.prompt()
def missed_call_followup() -> str:
    """Work through unanswered calls and propose follow-ups."""
    return (
        "List calls with list_calls(status='no_answer'), look up each caller with "
        "search_customers, and draft a short follow-up per caller. Ask for "
        "confirmation before using send_whatsapp_message or trigger_call."
    )
