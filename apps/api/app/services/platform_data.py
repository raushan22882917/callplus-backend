"""Customer data export (portability) and full erasure (right to be
forgotten)."""

import csv
import io
import json
import uuid
import zipfile
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.core.exceptions import NotFoundError
from sqlalchemy import delete, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    Conversation,
    Customer,
    CustomerActivity,
    Message,
    Ticket,
    TicketComment,
)


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def row_to_dict(row: Any) -> dict[str, Any]:
    return {c.key: _plain(getattr(row, c.key)) for c in inspect(row).mapper.column_attrs}


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    if rows:
        fields = list(rows[0].keys())
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()}
            )
    return buffer.getvalue().encode("utf-8")


async def _customer(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> Customer:
    customer = (
        await db.execute(
            select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("Customer not found")
    return customer


async def _conversation_ids(
    db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, call_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    conditions = [Conversation.customer_id == customer_id]
    if call_ids:
        conditions.append(Conversation.call_id.in_(call_ids))
    return list(
        (
            await db.execute(
                select(Conversation.id).where(Conversation.tenant_id == tenant_id, or_(*conditions))
            )
        )
        .scalars()
        .all()
    )


async def collect_customer_data(
    db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> dict[str, list[dict[str, Any]]]:
    customer = await _customer(db, tenant_id, customer_id)
    calls = (
        (
            await db.execute(
                select(Call).where(Call.tenant_id == tenant_id, Call.customer_id == customer_id)
            )
        )
        .scalars()
        .all()
    )
    conversation_ids = await _conversation_ids(db, tenant_id, customer_id, [c.id for c in calls])
    conversations = (
        (await db.execute(select(Conversation).where(Conversation.id.in_(conversation_ids))))
        .scalars()
        .all()
        if conversation_ids
        else []
    )
    messages = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.tenant_id == tenant_id, Message.conversation_id.in_(conversation_ids)
                )
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
        if conversation_ids
        else []
    )
    activities = (
        (
            await db.execute(
                select(CustomerActivity).where(
                    CustomerActivity.tenant_id == tenant_id,
                    CustomerActivity.customer_id == customer_id,
                )
            )
        )
        .scalars()
        .all()
    )
    tickets = (
        (
            await db.execute(
                select(Ticket).where(
                    Ticket.tenant_id == tenant_id, Ticket.customer_id == customer_id
                )
            )
        )
        .scalars()
        .all()
    )
    comments = (
        (
            await db.execute(
                select(TicketComment).where(
                    TicketComment.tenant_id == tenant_id,
                    TicketComment.ticket_id.in_([t.id for t in tickets]),
                )
            )
        )
        .scalars()
        .all()
        if tickets
        else []
    )
    return {
        "customer": [row_to_dict(customer)],
        "calls": [row_to_dict(r) for r in calls],
        "conversations": [row_to_dict(r) for r in conversations],
        "messages": [row_to_dict(r) for r in messages],
        "activities": [row_to_dict(r) for r in activities],
        "tickets": [row_to_dict(r) for r in tickets],
        "ticket_comments": [row_to_dict(r) for r in comments],
    }


def build_export_zip(data: dict[str, list[dict[str, Any]]]) -> bytes:
    """One JSON file with everything, plus a CSV per collection."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export.json", json.dumps(data, indent=2, default=str))
        for name, rows in data.items():
            archive.writestr(f"{name}.csv", _csv_bytes(rows))
    return out.getvalue()


async def erase_customer(
    db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> dict[str, int]:
    """Deletes the customer and everything tied to them: messages and
    conversations, calls (and via FK cascade their events), activities,
    tickets and comments. Payment records are financial records and are kept
    but unlinked (their customer FK is SET NULL). Caller audit-logs and commits."""
    await _customer(db, tenant_id, customer_id)
    call_ids = list(
        (
            await db.execute(
                select(Call.id).where(Call.tenant_id == tenant_id, Call.customer_id == customer_id)
            )
        )
        .scalars()
        .all()
    )
    conversation_ids = await _conversation_ids(db, tenant_id, customer_id, call_ids)
    ticket_ids = list(
        (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.tenant_id == tenant_id, Ticket.customer_id == customer_id
                )
            )
        )
        .scalars()
        .all()
    )

    counts: dict[str, int] = {}

    async def run(name: str, statement) -> None:
        result = await db.execute(statement)
        counts[name] = result.rowcount or 0

    if conversation_ids:
        await run("messages", delete(Message).where(Message.conversation_id.in_(conversation_ids)))
        await run(
            "conversations", delete(Conversation).where(Conversation.id.in_(conversation_ids))
        )
    if ticket_ids:
        await run(
            "ticket_comments", delete(TicketComment).where(TicketComment.ticket_id.in_(ticket_ids))
        )
    await run(
        "tickets",
        delete(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.customer_id == customer_id),
    )
    await run(
        "calls", delete(Call).where(Call.tenant_id == tenant_id, Call.customer_id == customer_id)
    )
    await run(
        "activities",
        delete(CustomerActivity).where(
            CustomerActivity.tenant_id == tenant_id, CustomerActivity.customer_id == customer_id
        ),
    )
    await run(
        "customers",
        delete(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id),
    )
    return counts
