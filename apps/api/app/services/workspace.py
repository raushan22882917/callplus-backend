"""Workspace read-side logic: the call handoff package (what a human sees when a
call lands on them), the supervisor live board, and the human-control gate the
AI turn endpoint checks."""

import uuid
from datetime import UTC, datetime

from app.schemas.workspace import (
    HandoffCustomer,
    HandoffMessage,
    HandoffPackage,
    HandoffPreviousCall,
    HandoffTicket,
    LiveBoard,
    LiveCall,
    SlaBreach,
    SuggestedAction,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallStatus,
    Conversation,
    Customer,
    Message,
    MessageSender,
    Ticket,
    TicketStatus,
    User,
)
from packages.database.models.workspace import (
    AgentProfile,
    CallHandoff,
    HandlerState,
)

LIVE_STATUSES = [CallStatus.RINGING, CallStatus.IN_PROGRESS]
OPEN_TICKET_STATUSES = [TicketStatus.OPEN, TicketStatus.PENDING]
# Hours an open ticket may wait before it counts as an SLA breach.
SLA_POLICY_HOURS = {"urgent": 1, "high": 4, "medium": 24, "low": 72}


async def get_handoff(db: AsyncSession, call_id: uuid.UUID) -> CallHandoff | None:
    return (
        await db.execute(select(CallHandoff).where(CallHandoff.call_id == call_id))
    ).scalar_one_or_none()


async def is_human_controlled(db: AsyncSession, call_id: uuid.UUID) -> bool:
    handoff = await get_handoff(db, call_id)
    return handoff is not None and handoff.state == HandlerState.HUMAN


def suggest_next_action(
    *,
    action_items: list[str],
    outcome: str | None,
    sentiment: str | None,
    open_tickets: list[Ticket],
    customer_name: str | None,
) -> SuggestedAction:
    """Rule-based, deterministic suggestion in priority order."""
    if action_items:
        return SuggestedAction(label=action_items[0], reason="Follow-up identified by call analysis")
    if outcome == "callback_requested":
        return SuggestedAction(
            label="Confirm a callback time with the customer", reason="Caller asked for a callback"
        )
    if open_tickets:
        return SuggestedAction(
            label=f"Review open ticket: {open_tickets[0].subject}",
            reason=f"{len(open_tickets)} unresolved ticket(s) for this customer",
        )
    if sentiment == "negative":
        return SuggestedAction(
            label="Acknowledge the frustration first, then offer a concrete resolution",
            reason="Caller sentiment is negative",
        )
    name = f" {customer_name}" if customer_name else ""
    return SuggestedAction(
        label=f"Greet{name} and confirm what they need", reason="No prior context to act on"
    )


async def build_handoff_package(
    db: AsyncSession, tenant_id: uuid.UUID, call: Call
) -> HandoffPackage:
    customer = await db.get(Customer, call.customer_id) if call.customer_id else None
    if customer is not None and customer.tenant_id != tenant_id:
        customer = None

    conversation = (
        await db.execute(select(Conversation).where(Conversation.call_id == call.id))
    ).scalar_one_or_none()
    messages: list[Message] = []
    if conversation is not None:
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

    previous: list[Call] = []
    tickets: list[Ticket] = []
    if customer is not None:
        previous = list(
            (
                await db.execute(
                    select(Call)
                    .where(
                        Call.tenant_id == tenant_id,
                        Call.customer_id == customer.id,
                        Call.id != call.id,
                    )
                    .order_by(Call.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        tickets = list(
            (
                await db.execute(
                    select(Ticket)
                    .where(Ticket.tenant_id == tenant_id, Ticket.customer_id == customer.id)
                    .order_by(Ticket.created_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    open_tickets = [t for t in tickets if t.status in OPEN_TICKET_STATUSES]
    action_items = [str(i) for i in (call.action_items or [])]
    handoff = await get_handoff(db, call.id)

    return HandoffPackage(
        call_id=call.id,
        status=call.status.value,
        direction=call.direction.value,
        from_number=call.from_number,
        to_number=call.to_number,
        started_at=call.started_at,
        handler=handoff.state if handoff else HandlerState.AI,
        handler_user_id=handoff.handler_user_id if handoff else None,
        customer=HandoffCustomer(
            id=customer.id,
            name=customer.name,
            phone=customer.phone,
            email=customer.email,
            tags=list(customer.tags or []),
            is_vip=any(t.strip().lower() == "vip" for t in customer.tags or []),
        )
        if customer
        else None,
        transcript=[
            HandoffMessage(
                sender="caller" if m.sender == MessageSender.CUSTOMER else m.sender.value,
                text=m.text,
                at=m.created_at,
            )
            for m in messages
        ],
        summary=call.summary,
        sentiment=call.sentiment,
        intent=call.intent,
        outcome=call.outcome,
        action_items=action_items,
        previous_calls=[
            HandoffPreviousCall(
                id=c.id,
                started_at=c.started_at,
                duration_seconds=c.duration_seconds,
                summary=c.summary,
                sentiment=c.sentiment,
                outcome=c.outcome,
            )
            for c in previous
        ],
        tickets=[
            HandoffTicket(
                id=t.id,
                subject=t.subject,
                status=t.status.value,
                priority=t.priority.value,
                created_at=t.created_at,
            )
            for t in tickets
        ],
        suggested_next_action=suggest_next_action(
            action_items=action_items,
            outcome=call.outcome,
            sentiment=call.sentiment,
            open_tickets=open_tickets,
            customer_name=customer.name if customer else None,
        ),
        recording_url=call.recording_url,
    )


async def build_live_board(
    db: AsyncSession, tenant_id: uuid.UUID, now: datetime | None = None
) -> LiveBoard:
    now = now or datetime.now(UTC)
    rows = (
        await db.execute(
            select(Call, Customer.name, CallHandoff, User.full_name)
            .outerjoin(Customer, Customer.id == Call.customer_id)
            .outerjoin(CallHandoff, CallHandoff.call_id == Call.id)
            .outerjoin(User, User.id == CallHandoff.handler_user_id)
            .where(Call.tenant_id == tenant_id, Call.status.in_(LIVE_STATUSES))
            .order_by(Call.started_at.asc().nulls_last(), Call.created_at.asc())
        )
    ).all()

    calls: list[LiveCall] = []
    for call, customer_name, handoff, handler_name in rows:
        human = handoff is not None and handoff.state == HandlerState.HUMAN
        started = call.started_at or call.created_at
        calls.append(
            LiveCall(
                id=call.id,
                status=call.status.value,
                direction=call.direction.value,
                customer_name=customer_name,
                from_number=call.from_number,
                started_at=call.started_at,
                seconds_elapsed=max(int((now - started).total_seconds()), 0),
                sentiment=call.sentiment,
                intent=call.intent,
                handler=HandlerState.HUMAN if human else HandlerState.AI,
                handler_user_id=handoff.handler_user_id if human else None,
                handler_name=handler_name if human else None,
                waiting=call.status == CallStatus.RINGING,
            )
        )

    status_rows = (
        await db.execute(
            select(AgentProfile.status, func.count())
            .where(AgentProfile.tenant_id == tenant_id)
            .group_by(AgentProfile.status)
        )
    ).all()
    agents_by_status = {"available": 0, "busy": 0, "away": 0, "offline": 0}
    for status_value, count in status_rows:
        agents_by_status[status_value.value] = count

    open_tickets = (
        (
            await db.execute(
                select(Ticket).where(
                    Ticket.tenant_id == tenant_id, Ticket.status.in_(OPEN_TICKET_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    breaches: list[SlaBreach] = []
    for ticket in open_tickets:
        limit = SLA_POLICY_HOURS[ticket.priority.value]
        age = (now - ticket.created_at).total_seconds() / 3600
        if age > limit:
            breaches.append(
                SlaBreach(
                    ticket_id=ticket.id,
                    subject=ticket.subject,
                    priority=ticket.priority.value,
                    age_hours=round(age, 1),
                    limit_hours=limit,
                )
            )
    breaches.sort(key=lambda b: b.age_hours - b.limit_hours, reverse=True)

    return LiveBoard(
        calls=calls,
        ai_count=sum(1 for c in calls if c.handler == HandlerState.AI),
        human_count=sum(1 for c in calls if c.handler == HandlerState.HUMAN),
        waiting_count=sum(1 for c in calls if c.waiting),
        agents_by_status=agents_by_status,
        sla_breaches=breaches[:20],
        sla_policy_hours=SLA_POLICY_HOURS,
    )
