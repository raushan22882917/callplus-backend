"""Transcript search: case-insensitive substring (ILIKE) match over call
messages, grouped per call with a highlighted snippet around the first hit."""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, Conversation, Customer, Message, MessageSender


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def make_snippet(text: str, query: str, radius: int = 70) -> dict | None:
    """Returns {"before", "match", "after"} around the first case-insensitive hit, else None."""
    m = re.search(re.escape(query), text, re.IGNORECASE)
    if not m:
        return None
    start = max(0, m.start() - radius)
    end = min(len(text), m.end() + radius)
    return {
        "before": ("…" if start > 0 else "") + text[start : m.start()],
        "match": text[m.start() : m.end()],
        "after": text[m.end() : end] + ("…" if end < len(text) else ""),
    }


async def search_transcripts(
    db: AsyncSession, tenant_id: uuid.UUID, query: str, *, limit: int = 20, max_messages: int = 400
) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    rows = (
        await db.execute(
            select(Message, Call, Customer.name)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Call, Call.id == Conversation.call_id)
            .outerjoin(Customer, Customer.id == Call.customer_id)
            .where(
                Message.tenant_id == tenant_id,
                Call.tenant_id == tenant_id,
                Message.text.ilike(f"%{escape_like(query)}%", escape="\\"),
            )
            .order_by(Call.created_at.desc(), Message.created_at.asc())
            .limit(max_messages)
        )
    ).all()
    grouped: dict[uuid.UUID, dict] = {}
    for message, call, customer_name in rows:
        entry = grouped.get(call.id)
        if entry is None:
            if len(grouped) >= limit:
                continue
            entry = grouped[call.id] = {
                "call_id": str(call.id),
                "created_at": call.created_at.isoformat(),
                "direction": call.direction.value,
                "customer_name": customer_name,
                "from_number": call.from_number,
                "sentiment": call.sentiment,
                "outcome": call.outcome,
                "match_count": 0,
                "snippet": None,
                "speaker": None,
            }
        entry["match_count"] += 1
        if entry["snippet"] is None:
            entry["snippet"] = make_snippet(message.text or "", query)
            entry["speaker"] = "caller" if message.sender == MessageSender.CUSTOMER else "agent"
    return list(grouped.values())
