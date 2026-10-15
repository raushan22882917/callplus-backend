"""Customer-experience analytics. Everything here is transparent rules over
data we already store (call sentiment/outcome/intent, tickets, payments) — no
trained model. Each score returns the list of factors and points that produced
it so a user can see exactly why a customer is flagged.

Churn risk (0-100, sum of fired rules, clamped):
  +25 latest call sentiment negative            -10 latest call sentiment positive
  +20 at least half of the last 5 calls negative
  +10 sentiment worsened (avg of last 2 calls below avg of the 3 before)
  +10 / +20 one / two-or-more complaint outcomes in the last 90 days
  +10 an escalated outcome in the last 30 days
  +5 per open ticket (max 15)
  +10 silent for over 60 days after 3 or more calls
Buying intent (0-100):
  +30 first sale_interest outcome in the last 60 days, +15 each further (max +60 total)
  +20 booking_requested in the last 60 days
  +15 a buying keyword (price, quote, plan, demo, buy, order, ...) in a recent call intent
  +10 latest call sentiment positive, -15 if negative
  +10 contact within the last 14 days
Levels: >=60 high, >=30 medium, else low.
"""

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    Payment,
    PaymentLink,
    PaymentStatus,
    Ticket,
    TicketStatus,
)

SENTIMENT_VALUE = {"positive": 1, "neutral": 0, "negative": -1}
BUYING_KEYWORDS = (
    "price", "pricing", "quote", "quotation", "plan", "demo", "buy", "purchase", "order",
    "book", "subscribe", "upgrade", "interested", "cost", "offer",
)  # fmt: skip


@dataclass
class CallPoint:
    at: datetime
    sentiment: str | None
    outcome: str | None
    intent: str | None


def level(score: int) -> str:
    return "high" if score >= 60 else "medium" if score >= 30 else "low"


def _avg(points: list[CallPoint]) -> float:
    vals = [SENTIMENT_VALUE.get(p.sentiment or "", 0) for p in points]
    return sum(vals) / len(vals) if vals else 0.0


def churn_risk(calls: list[CallPoint], open_tickets: int, now: datetime) -> dict:
    """`calls` in any order. Returns {"score", "level", "factors": [{"factor","points"}]}."""
    factors: list[dict] = []
    ordered = sorted(calls, key=lambda c: c.at)

    def add(name: str, pts: int) -> None:
        factors.append({"factor": name, "points": pts})

    if ordered:
        latest = ordered[-1]
        if latest.sentiment == "negative":
            add("Latest call ended with negative sentiment", 25)
        elif latest.sentiment == "positive":
            add("Latest call ended with positive sentiment", -10)
        last5 = ordered[-5:]
        if sum(1 for c in last5 if c.sentiment == "negative") * 2 >= len(last5) and any(
            c.sentiment == "negative" for c in last5
        ):
            add("At least half of the last 5 calls were negative", 20)
        if len(ordered) >= 3:
            recent, earlier = ordered[-2:], ordered[-5:-2]
            if _avg(recent) < _avg(earlier):
                add("Sentiment is trending down", 10)
        complaints = sum(
            1 for c in ordered if c.outcome == "complaint" and c.at >= now - timedelta(days=90)
        )
        if complaints >= 2:
            add(f"{complaints} complaints in the last 90 days", 20)
        elif complaints == 1:
            add("A complaint in the last 90 days", 10)
        if any(c.outcome == "escalated" and c.at >= now - timedelta(days=30) for c in ordered):
            add("Call escalated in the last 30 days", 10)
        if len(ordered) >= 3 and latest.at < now - timedelta(days=60):
            add("No contact for over 60 days after repeat calls", 10)
    if open_tickets:
        add(f"{open_tickets} open ticket(s)", min(open_tickets * 5, 15))
    score = max(0, min(100, sum(f["points"] for f in factors)))
    return {"score": score, "level": level(score), "factors": factors}


def buying_intent(calls: list[CallPoint], now: datetime) -> dict:
    factors: list[dict] = []
    ordered = sorted(calls, key=lambda c: c.at)
    recent60 = [c for c in ordered if c.at >= now - timedelta(days=60)]
    sale = sum(1 for c in recent60 if c.outcome == "sale_interest")
    if sale:
        factors.append(
            {
                "factor": f"{sale} sale-interest call(s) in the last 60 days",
                "points": min(30 + 15 * (sale - 1), 60),
            }
        )
    if any(c.outcome == "booking_requested" for c in recent60):
        factors.append({"factor": "Requested a booking in the last 60 days", "points": 20})
    if any(c.intent and any(k in c.intent.lower() for k in BUYING_KEYWORDS) for c in recent60):
        factors.append({"factor": "Recent call intent mentions buying/pricing", "points": 15})
    if ordered:
        latest = ordered[-1]
        if latest.sentiment == "positive":
            factors.append({"factor": "Latest call ended positive", "points": 10})
        elif latest.sentiment == "negative":
            factors.append({"factor": "Latest call ended negative", "points": -15})
        if latest.at >= now - timedelta(days=14):
            factors.append({"factor": "Contacted in the last 14 days", "points": 10})
    score = max(0, min(100, sum(f["points"] for f in factors)))
    return {"score": score, "level": level(score), "factors": factors}


def complaint_severity(
    *, sentiment: str | None, outcome: str | None, complaints_90d: int
) -> tuple[str, list[str]]:
    """high: escalated complaint, or negative complaint from a repeat complainer;
    medium: any complaint outcome or escalation; low: just negative sentiment."""
    reasons = []
    if outcome == "complaint":
        reasons.append("outcome is complaint")
    if outcome == "escalated":
        reasons.append("call was escalated")
    if sentiment == "negative":
        reasons.append("caller ended negative")
    if complaints_90d >= 2:
        reasons.append(f"{complaints_90d} complaints from this customer in 90 days")
    if (outcome == "escalated" and sentiment == "negative") or (
        outcome == "complaint" and sentiment == "negative" and complaints_90d >= 2
    ):
        return "high", reasons
    if outcome in ("complaint", "escalated"):
        return "medium", reasons
    return "low", reasons


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _points(rows) -> list[CallPoint]:
    return [CallPoint(r.created_at, r.sentiment, r.outcome, r.intent) for r in rows]


async def sentiment_trend(db: AsyncSession, tenant_id: uuid.UUID, days: int) -> list[dict]:
    day = func.date(Call.created_at)
    rows = (
        await db.execute(
            select(day.label("day"), Call.sentiment, func.count())
            .where(Call.tenant_id == tenant_id, Call.created_at >= _since(days), Call.sentiment.is_not(None))
            .group_by(day, Call.sentiment)
            .order_by(day)
        )
    ).all()
    by_day: dict = {}
    for d, sentiment, count in rows:
        entry = by_day.setdefault(
            d, {"day": d.isoformat(), "positive": 0, "neutral": 0, "negative": 0}
        )
        if sentiment in SENTIMENT_VALUE:
            entry[sentiment] += count
    out = []
    for entry in by_day.values():
        total = entry["positive"] + entry["neutral"] + entry["negative"]
        entry["total"] = total
        entry["score"] = round((entry["positive"] - entry["negative"]) / total, 2) if total else 0
        out.append(entry)
    return out


async def intent_distribution(
    db: AsyncSession, tenant_id: uuid.UUID, days: int, limit: int = 15
) -> list[dict]:
    key = func.lower(func.trim(Call.intent))
    rows = (
        await db.execute(
            select(key.label("intent"), func.count().label("n"))
            .where(Call.tenant_id == tenant_id, Call.created_at >= _since(days), Call.intent.is_not(None))
            .group_by(key)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return [{"intent": r.intent, "count": r.n} for r in rows if r.intent]


async def complaints(db: AsyncSession, tenant_id: uuid.UUID, days: int, limit: int = 50) -> dict:
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(Call, Customer.name)
            .outerjoin(Customer, Customer.id == Call.customer_id)
            .where(
                Call.tenant_id == tenant_id,
                Call.created_at >= _since(days),
                (Call.outcome.in_(["complaint", "escalated"])) | (Call.sentiment == "negative"),
            )
            .order_by(Call.created_at.desc())
            .limit(500)
        )
    ).all()
    per_customer = Counter(
        c.customer_id
        for c, _ in rows
        if c.outcome == "complaint" and c.customer_id and c.created_at >= now - timedelta(days=90)
    )
    items = []
    for call, name in rows:
        severity, reasons = complaint_severity(
            sentiment=call.sentiment,
            outcome=call.outcome,
            complaints_90d=per_customer.get(call.customer_id, 0),
        )
        items.append(
            {
                "call_id": str(call.id),
                "customer_id": str(call.customer_id) if call.customer_id else None,
                "customer_name": name,
                "created_at": call.created_at.isoformat(),
                "summary": call.summary,
                "intent": call.intent,
                "sentiment": call.sentiment,
                "outcome": call.outcome,
                "severity": severity,
                "reasons": reasons,
            }
        )
    counts = Counter(i["severity"] for i in items)
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: (order[i["severity"]], i["created_at"]), reverse=False)
    return {
        "counts": {k: counts.get(k, 0) for k in ("high", "medium", "low")},
        "items": items[:limit],
    }


async def _open_tickets_by_customer(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    rows = (
        await db.execute(
            select(Ticket.customer_id, func.count())
            .where(
                Ticket.tenant_id == tenant_id,
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.PENDING]),
            )
            .group_by(Ticket.customer_id)
        )
    ).all()
    return {cid: n for cid, n in rows}


async def customer_scores(
    db: AsyncSession, tenant_id: uuid.UUID, *, sort: str = "churn", limit: int = 50
) -> list[dict]:
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(Call.customer_id, Call.created_at, Call.sentiment, Call.outcome, Call.intent)
            .where(
                Call.tenant_id == tenant_id,
                Call.customer_id.is_not(None),
                Call.created_at >= _since(365),
                Call.analyzed_at.is_not(None),
            )
            .order_by(Call.created_at.asc())
        )
    ).all()
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r.customer_id, []).append(r)
    open_tickets = await _open_tickets_by_customer(db, tenant_id)
    names = {}
    if grouped:
        names = {
            cid: (name, phone)
            for cid, name, phone in (
                await db.execute(
                    select(Customer.id, Customer.name, Customer.phone).where(
                        Customer.tenant_id == tenant_id, Customer.id.in_(list(grouped))
                    )
                )
            ).all()
        }
    out = []
    for cid, call_rows in grouped.items():
        pts = _points(call_rows)
        churn = churn_risk(pts, open_tickets.get(cid, 0), now)
        buy = buying_intent(pts, now)
        name, phone = names.get(cid, (None, None))
        out.append(
            {
                "customer_id": str(cid),
                "name": name,
                "phone": phone,
                "calls": len(pts),
                "last_contact": max(p.at for p in pts).isoformat(),
                "churn_risk": churn,
                "buying_intent": buy,
            }
        )
    key = "churn_risk" if sort == "churn" else "buying_intent"
    out.sort(key=lambda r: r[key]["score"], reverse=True)
    return out[:limit]


async def single_customer_scores(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> dict:
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(Call.created_at, Call.sentiment, Call.outcome, Call.intent).where(
                Call.tenant_id == tenant_id,
                Call.customer_id == customer_id,
                Call.analyzed_at.is_not(None),
            )
        )
    ).all()
    pts = _points(rows)
    tickets = (await _open_tickets_by_customer(db, tenant_id)).get(customer_id, 0)
    return {
        "customer_id": str(customer_id),
        "calls": len(pts),
        "churn_risk": churn_risk(pts, tickets, now),
        "buying_intent": buying_intent(pts, now),
    }


async def journey(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, limit: int = 200) -> list[dict]:
    """Calls, WhatsApp threads, tickets and payments for one customer, newest first.
    (There is no appointments table in this product yet, so none are listed.)"""
    events: list[dict] = []
    for call in (
        await db.execute(select(Call).where(Call.tenant_id == tenant_id, Call.customer_id == customer_id))
    ).scalars():
        events.append(
            {
                "type": "call",
                "at": call.created_at.isoformat(),
                "title": f"{call.direction.value.title()} call ({call.status.value})",
                "detail": call.summary or call.intent,
                "sentiment": call.sentiment,
                "reference_id": str(call.id),
            }
        )
    wa = (
        await db.execute(
            select(Conversation.id, Conversation.created_at, func.count(Message.id), func.max(Message.created_at))
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.customer_id == customer_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
            )
            .group_by(Conversation.id)
        )
    ).all()
    for cid, created, n, last in wa:
        events.append(
            {
                "type": "whatsapp",
                "at": (last or created).isoformat(),
                "title": f"WhatsApp conversation ({n} messages)",
                "detail": None,
                "sentiment": None,
                "reference_id": str(cid),
            }
        )
    for t in (
        await db.execute(select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.customer_id == customer_id))
    ).scalars():
        events.append(
            {
                "type": "ticket",
                "at": t.created_at.isoformat(),
                "title": f"Ticket: {t.subject}",
                "detail": f"{t.status.value}, {t.priority.value} priority",
                "sentiment": None,
                "reference_id": str(t.id),
            }
        )
    pay_rows = (
        await db.execute(
            select(Payment, PaymentLink.customer_id)
            .join(PaymentLink, PaymentLink.id == Payment.payment_link_id)
            .where(Payment.tenant_id == tenant_id, PaymentLink.customer_id == customer_id)
        )
    ).all()
    for p, _ in pay_rows:
        events.append(
            {
                "type": "payment",
                "at": p.created_at.isoformat(),
                "title": f"Payment {p.status.value}: {p.currency} {p.amount_paise / 100:.2f}",
                "detail": p.method,
                "sentiment": None,
                "reference_id": str(p.id),
            }
        )
    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]


async def repeat_callers(db: AsyncSession, tenant_id: uuid.UUID, days: int, min_calls: int = 2, limit: int = 50) -> list[dict]:
    """Callers (by phone number, inbound `from_number`) with at least `min_calls` calls in the window."""
    num = func.coalesce(Call.from_number, "unknown")
    rows = (
        await db.execute(
            select(
                num.label("number"),
                func.count().label("n"),
                func.max(Call.created_at).label("last"),
                func.count().filter(Call.outcome == "complaint").label("complaints"),
                func.max(Call.customer_id.cast(String)).label("customer_id"),
            )
            .where(
                Call.tenant_id == tenant_id,
                Call.direction == CallDirection.INBOUND,
                Call.created_at >= _since(days),
                Call.from_number.is_not(None),
            )
            .group_by(num)
            .having(func.count() >= min_calls)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    names = {}
    ids = [uuid.UUID(r.customer_id) for r in rows if r.customer_id]
    if ids:
        names = {
            str(i): n
            for i, n in (
                await db.execute(select(Customer.id, Customer.name).where(Customer.tenant_id == tenant_id, Customer.id.in_(ids)))
            ).all()
        }
    return [
        {
            "phone": r.number,
            "customer_id": r.customer_id,
            "customer_name": names.get(r.customer_id) if r.customer_id else None,
            "call_count": r.n,
            "complaint_count": r.complaints,
            "last_call": r.last.isoformat(),
        }
        for r in rows
    ]


async def customer_value(db: AsyncSession, tenant_id: uuid.UUID, limit: int = 50) -> list[dict]:
    """Lifetime value from captured payments (refunded payments are subtracted) per customer."""
    net = func.sum(
        case((Payment.status == PaymentStatus.CAPTURED, Payment.amount_paise), else_=0)
    ) - func.sum(case((Payment.status == PaymentStatus.REFUNDED, Payment.amount_paise), else_=0))
    rows = (
        await db.execute(
            select(
                PaymentLink.customer_id,
                Customer.name,
                net.label("net"),
                func.count().filter(Payment.status == PaymentStatus.CAPTURED).label("payments"),
                func.max(Payment.created_at).label("last"),
            )
            .join(PaymentLink, PaymentLink.id == Payment.payment_link_id)
            .join(Customer, Customer.id == PaymentLink.customer_id)
            .where(Payment.tenant_id == tenant_id)
            .group_by(PaymentLink.customer_id, Customer.name)
            .order_by(net.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "customer_id": str(r.customer_id),
            "name": r.name,
            "net_paise": int(r.net or 0),
            "payments": r.payments,
            "last_payment": r.last.isoformat() if r.last else None,
        }
        for r in rows
    ]
