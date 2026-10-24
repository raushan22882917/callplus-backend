"""Prepaid wallet: top up in rupees, pay per answered minute and per message.

Money is integer paise throughout. A top-up buys the rate for the minutes that follow, and
the bigger the top-up, the lower the rate; the rates stay until the next top-up. Usage is
settled against the balance from what already exists (calls, outbound WhatsApp messages,
usage events), each charged once through the ledger's unique reference, so a retry or a
second worker can never charge twice. The balance may dip below zero by the cost of the
call that was in progress; the next call is refused until the wallet is topped up.

The rate table is the one place to change prices. The defaults leave a margin over today's
cost (about INR 2.9 inbound and 6.6 outbound per minute, docs/BILLING.md); lower them as
the cost per minute comes down."""

import html
import logging
import math
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    Conversation,
    ConversationChannel,
    Message,
    MessageDirection,
    Tenant,
    UsageEvent,
    Wallet,
    WalletTopup,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

TOPUP_MIN_PAISE = 10_000  # INR 100
TOPUP_MAX_PAISE = 5_000_000  # INR 50,000
GST_PERCENT = 18
LOW_BALANCE_PAISE = 20_000
WHATSAPP_MESSAGE_PAISE = 150
SMS_PAISE = 75

# (smallest top-up in paise, inbound paise per minute, outbound paise per minute)
RATE_TIERS: tuple[tuple[int, int, int], ...] = (
    (10_000, 500, 900),
    (100_000, 450, 800),
    (500_000, 400, 750),
    (2_000_000, 350, 700),
)
_BATCH = 500


def rates_for_topup(amount_paise: int) -> tuple[int, int]:
    """(inbound, outbound) paise per minute earned by a top-up of this size."""
    chosen = RATE_TIERS[0]
    for tier in RATE_TIERS:
        if amount_paise >= tier[0]:
            chosen = tier
    return chosen[1], chosen[2]


def rate_table() -> list[dict]:
    top = TOPUP_MAX_PAISE
    out = []
    for i, (low, inbound, outbound) in enumerate(RATE_TIERS):
        high = RATE_TIERS[i + 1][0] - 100 if i + 1 < len(RATE_TIERS) else top
        out.append(
            {"from_paise": low, "to_paise": high, "inbound_paise": inbound, "outbound_paise": outbound}
        )
    return out


# ---- GST ------------------------------------------------------------------------------------

_GSTIN = re.compile(r"^(\d{2})[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z([0-9A-Z])$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def validate_gstin(gstin: str) -> str:
    """Returns the normalised GSTIN or raises ValueError. Checks the format, the state code
    and the check character."""
    value = (gstin or "").strip().upper()
    match = _GSTIN.match(value)
    if not match or not 1 <= int(match.group(1)) <= 38:
        raise ValueError("That GSTIN is not valid")
    total = 0
    for i, ch in enumerate(value[:14]):
        n = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += n // 36 + n % 36
    if _CHARS[(36 - total % 36) % 36] != value[14]:
        raise ValueError("That GSTIN is not valid (check character does not match)")
    return value


def gst_split(amount_paise: int, buyer_state: str | None, seller_state: str | None) -> dict[str, int]:
    """18% GST on top of the amount: CGST+SGST when buyer and seller are in the same state,
    otherwise IGST."""
    gst = round(amount_paise * GST_PERCENT / 100)
    if buyer_state and seller_state and buyer_state == seller_state:
        cgst = gst // 2
        return {"cgst": cgst, "sgst": gst - cgst, "igst": 0, "gst": gst, "total": amount_paise + gst}
    return {"cgst": 0, "sgst": 0, "igst": gst, "gst": gst, "total": amount_paise + gst}


def financial_year(when: datetime) -> str:
    start = when.year if when.month >= 4 else when.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


# ---- the wallet and its ledger --------------------------------------------------------------


async def get_wallet(db: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False) -> Wallet | None:
    stmt = select(Wallet).where(Wallet.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def ensure_wallet(db: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False) -> Wallet:
    wallet = await get_wallet(db, tenant_id, lock=lock)
    if wallet is None:
        inbound, outbound = rates_for_topup(0)
        wallet = Wallet(
            tenant_id=tenant_id,
            balance_paise=0,
            inbound_rate_paise=inbound,
            outbound_rate_paise=outbound,
            low_balance_paise=LOW_BALANCE_PAISE,
            billing_started_at=datetime.now(UTC),
        )
        db.add(wallet)
        await db.flush()
    return wallet


def _post(
    db: AsyncSession,
    wallet: Wallet,
    *,
    kind: str,
    amount_paise: int,
    reference_id: uuid.UUID | None,
    description: str,
    detail: dict | None = None,
) -> None:
    wallet.balance_paise += amount_paise
    db.add(
        WalletTransaction(
            tenant_id=wallet.tenant_id,
            kind=kind,
            amount_paise=amount_paise,
            balance_after_paise=wallet.balance_paise,
            reference_id=reference_id,
            description=description[:255],
            detail=detail or {},
        )
    )


async def settle(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Charges the wallet for usage not yet charged. Returns the paise charged.
    Safe to call as often as you like, from any process."""
    wallet = await get_wallet(db, tenant_id, lock=True)
    if wallet is None:
        return 0
    before = wallet.balance_paise
    since = wallet.billing_started_at

    when = func.coalesce(Call.started_at, Call.created_at)
    calls = (
        await db.execute(
            select(Call)
            .where(
                Call.tenant_id == tenant_id,
                Call.ai_agent_id.is_not(None),
                Call.duration_seconds > 0,
                when >= since,
                Call.id.not_in(
                    select(WalletTransaction.reference_id).where(
                        WalletTransaction.tenant_id == tenant_id,
                        WalletTransaction.kind == "call",
                        WalletTransaction.reference_id.is_not(None),
                    )
                ),
            )
            .order_by(when)
            .limit(_BATCH)
        )
    ).scalars().all()
    for call in calls:
        minutes = math.ceil(call.duration_seconds / 60)
        inbound = call.direction == CallDirection.INBOUND
        rate = wallet.inbound_rate_paise if inbound else wallet.outbound_rate_paise
        _post(
            db,
            wallet,
            kind="call",
            amount_paise=-minutes * rate,
            reference_id=call.id,
            description=f"{'Inbound' if inbound else 'Outbound'} call, {minutes} min",
            detail={"minutes": minutes, "rate_paise": rate, "direction": call.direction.value},
        )

    messages = (
        await db.execute(
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Conversation.channel == ConversationChannel.WHATSAPP,
                Message.direction == MessageDirection.OUTBOUND,
                Message.created_at >= since,
                Message.id.not_in(
                    select(WalletTransaction.reference_id).where(
                        WalletTransaction.tenant_id == tenant_id,
                        WalletTransaction.kind == "message",
                        WalletTransaction.reference_id.is_not(None),
                    )
                ),
            )
            .limit(_BATCH)
        )
    ).scalars().all()
    for message_id in messages:
        _post(db, wallet, kind="message", amount_paise=-WHATSAPP_MESSAGE_PAISE, reference_id=message_id,
              description="WhatsApp message", detail={"channel": "whatsapp"})

    events = (
        await db.execute(
            select(UsageEvent)
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.metric.in_(("whatsapp_messages", "sms")),
                UsageEvent.occurred_at >= since,
                UsageEvent.id.not_in(
                    select(WalletTransaction.reference_id).where(
                        WalletTransaction.tenant_id == tenant_id,
                        WalletTransaction.kind == "message",
                        WalletTransaction.reference_id.is_not(None),
                    )
                ),
            )
            .limit(_BATCH)
        )
    ).scalars().all()
    for event in events:
        unit = WHATSAPP_MESSAGE_PAISE if event.metric == "whatsapp_messages" else SMS_PAISE
        count = max(1, math.ceil(float(event.quantity)))
        label = "WhatsApp message" if event.metric == "whatsapp_messages" else "SMS"
        _post(db, wallet, kind="message", amount_paise=-count * unit, reference_id=event.id,
              description=f"{label}{'s' if count > 1 else ''}" + (f" x{count}" if count > 1 else ""),
              detail={"channel": event.metric, "count": count})
    await db.flush()
    return before - wallet.balance_paise


async def credit_topup(db: AsyncSession, topup: WalletTopup, payment_id: str) -> bool:
    """Adds a paid top-up to the wallet, once. Returns False if it was already applied
    (a webhook that arrives after the browser confirmed, or a retry)."""
    topup = (
        await db.execute(select(WalletTopup).where(WalletTopup.id == topup.id).with_for_update())
    ).scalar_one()
    if topup.status == "paid":
        return False
    wallet = await ensure_wallet(db, topup.tenant_id, lock=True)
    now = datetime.now(UTC)
    first = wallet.balance_paise == 0 and not (
        await db.execute(
            select(WalletTransaction.id).where(
                WalletTransaction.tenant_id == topup.tenant_id, WalletTransaction.kind == "topup"
            ).limit(1)
        )
    ).first()
    if first:
        wallet.billing_started_at = now  # usage before the first top-up belongs to the free plan
    wallet.inbound_rate_paise = topup.inbound_rate_paise
    wallet.outbound_rate_paise = topup.outbound_rate_paise
    _post(db, wallet, kind="topup", amount_paise=topup.amount_paise, reference_id=topup.id,
          description=f"Top-up {topup.invoice_number or ''}".strip(),
          detail={"payment_id": payment_id, "paid_paise": topup.total_paise})
    topup.status, topup.provider_payment_id, topup.paid_at = "paid", payment_id, now
    topup.invoice_number = await _next_invoice_number(db, now)
    tenant = await db.get(Tenant, topup.tenant_id)
    if tenant is not None and tenant.plan_tier in ("free", "payg"):
        tenant.plan_tier = "payg"
    await db.flush()
    return True


async def _next_invoice_number(db: AsyncSession, when: datetime) -> str:
    fy = financial_year(when)
    # One at a time per financial year, so two payments cannot take the same number.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"invoice-{fy}"})
    last = (
        await db.execute(
            select(func.count(WalletTopup.id)).where(WalletTopup.invoice_number.like(f"CP/{fy}/%"))
        )
    ).scalar_one()
    return f"CP/{fy}/{last + 1:06d}"


# ---- gates used by the plan checks ----------------------------------------------------------


def rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


async def ensure_balance(db: AsyncSession, tenant_id: uuid.UUID, need_paise: int, what: str) -> None:
    from app.services.plans import PlanLimitError

    await settle(db, tenant_id)
    wallet = await get_wallet(db, tenant_id)
    balance = wallet.balance_paise if wallet else 0
    if balance < need_paise:
        raise PlanLimitError(
            "wallet_empty",
            f"Your wallet balance is {rupees(max(balance, 0))}, not enough for {what}. "
            "Top up under Billing → Wallet.",
            tier="payg",
        )


async def minutes_available(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Whole minutes the balance pays for at the dearer (outbound) rate."""
    await settle(db, tenant_id)
    wallet = await get_wallet(db, tenant_id)
    if wallet is None or wallet.balance_paise <= 0:
        return 0
    return wallet.balance_paise // max(wallet.outbound_rate_paise, 1)


async def ensure_call_balance(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    wallet = await get_wallet(db, tenant_id)
    need = wallet.inbound_rate_paise if wallet else rates_for_topup(0)[0]
    await ensure_balance(db, tenant_id, need, "another call")


# ---- invoice --------------------------------------------------------------------------------


def invoice_html(topup: WalletTopup, seller: dict) -> str:
    e = html.escape

    def money(p: int) -> str:
        return rupees(p)

    taxes = []
    if topup.igst_paise:
        taxes.append(("IGST 18%", topup.igst_paise))
    else:
        taxes += [("CGST 9%", topup.cgst_paise), ("SGST 9%", topup.sgst_paise)]
    rows = "".join(f"<tr><td>{e(n)}</td><td class=r>{money(v)}</td></tr>" for n, v in taxes)
    paid = topup.paid_at.strftime("%d %b %Y") if topup.paid_at else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Invoice {e(topup.invoice_number or '')}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;color:#0b1b2b;max-width:680px;margin:32px auto;padding:0 20px}}
h1{{font-size:20px;margin:0 0 4px}}table{{width:100%;border-collapse:collapse;margin:16px 0}}td,th{{padding:8px 6px;border-bottom:1px solid #dbe9e4;text-align:left}}
.r{{text-align:right}}.muted{{color:#475569}}.tot td{{font-weight:600;border-bottom:0}}</style></head><body>
<h1>Tax invoice {e(topup.invoice_number or '')}</h1><p class=muted>Date {e(paid)}</p>
<p><strong>{e(seller.get('name') or '')}</strong><br>{e(seller.get('address') or '')}<br>GSTIN {e(seller.get('gstin') or '—')}</p>
<p><strong>Billed to</strong><br>{e(topup.billing_name or '')}<br>GSTIN {e(topup.gstin or '—')} · State code {e(topup.state_code or '—')}</p>
<table><tr><th>Description</th><th class=r>Amount</th></tr>
<tr><td>Prepaid wallet credit (AI calling and messaging)</td><td class=r>{money(topup.amount_paise)}</td></tr>{rows}
<tr class=tot><td>Total paid</td><td class=r>{money(topup.total_paise)}</td></tr></table>
<p class=muted>Payment {e(topup.provider_payment_id or '')}. Wallet credit is used for answered call minutes and messages and does not expire.</p>
</body></html>"""
