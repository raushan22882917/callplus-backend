"""Channel packs: Call, WhatsApp, and Email, each paid for on its own, or all three together.

A pack is a 30 day allowance for one channel. A workspace holds any mix of them. Buying all three in one
payment takes `pack_bundle_discount_percent` (10) off the sum, and nothing less than all three gets a discount.

How it stays safe
* Prices and allowances live here. A request only names which packs it wants; it never sends an amount.
* A purchase is a row (`pack_purchases`) with the price, tax and allowances fixed when the order was made.
* The workspace's live entitlements are `tenants.pack_state`, written only by `credit_purchase`, which runs once
  per paid order (row lock + status check), only after the provider's signature or capture is verified and the
  amount matches exactly.
* Allowances are hard caps measured inside the pack's own window, so usage before the purchase never counts and
  an expired pack allows nothing.
* A workspace on a monthly plan, pay-as-you-go or legacy cannot buy packs: two billing models never run at once.
"""

import html
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.services import plans, wallet
from app.services.platform_usage import aggregate_usage
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import PackPurchase, Tenant

PERIOD_DAYS = 30
CODES = ("call", "whatsapp", "email_sms")
# Plans a workspace may be on and still buy packs. Buying one moves a free workspace onto "packs".
BUYABLE_FROM = ("free", "packs")
MAX_OPEN_ORDERS_PER_HOUR = 10


@dataclass(frozen=True)
class Pack:
    code: str
    name: str
    price_inr: int  # per 30 days, before GST
    blurb: str
    allowances: dict  # what it grants for one period


# Prices are the lowest round figure at which even the worst case still earns a margin, and the email allowance is the
# most the Email price can hold at the stress cost; see docs/PRICING_ANALYSIS.md and `python scripts/cost_model.py`.
# The Email pack keeps the code "email_sms" because purchases and held packs are stored under it; it no longer includes SMS.
# tests/test_pack_economics.py fails if an edit here makes a pack lose money, so change them together with the model.
CATALOGUE: dict[str, Pack] = {
    "call": Pack(
        "call", "Call pack", 2199,
        "AI phone calls, in and out: 250 minutes, outbound campaigns and call recording.",
        {"ai_minutes": 250},
    ),
    "whatsapp": Pack(
        "whatsapp", "WhatsApp pack", 899,
        "1,000 WhatsApp messages.",
        {"whatsapp_messages": 1000},
    ),
    "email_sms": Pack(
        "email_sms", "Email pack", 799,
        "5,000 customer emails.",
        {"email_messages": 5000},
    ),
}

# What holding a call pack unlocks beyond minutes, and what any pack gives the workspace to build with.
_CALL_PERKS = {"max_call_seconds": 900, "max_concurrent_calls": 2, "daily_outbound_calls": 100, "phone_numbers": 2, "recording": True}
_ANY_PACK_PERKS = {"agents": 3, "team_members": 5, "knowledge_documents": 25, "api_keys": 3, "campaigns": True}


class PackError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


# ---- prices ---------------------------------------------------------------------------------


def bundle_discount_percent() -> int:
    return max(0, min(int(get_settings().pack_bundle_discount_percent), 50))


def quote(items: list[str]) -> dict:
    """What these packs cost, worked out here and nowhere else. Raises PackError for a bad choice."""
    chosen = list(dict.fromkeys(items))
    if not chosen or any(code not in CODES for code in chosen):
        raise PackError("Choose from: call, whatsapp, email_sms.")
    full = sum(CATALOGUE[code].price_inr for code in chosen) * 100
    bundle = set(chosen) == set(CODES)
    discount = round(full * bundle_discount_percent() / 100) if bundle else 0
    amount = full - discount
    allowances = {code: dict(CATALOGUE[code].allowances) for code in chosen}
    return {
        "items": [code for code in CODES if code in chosen],
        "bundle": bundle,
        "list_price_paise": full,
        "discount_paise": discount,
        "amount_paise": amount,  # before GST
        "allowances": allowances,
        "period_days": PERIOD_DAYS,
    }


def catalogue(buyer_state: str | None = None) -> dict:
    seller = get_settings().seller_state_code or None
    rows = []
    for code in CODES:
        q = quote([code])
        tax = wallet.gst_split(q["amount_paise"], buyer_state, seller)
        pack = CATALOGUE[code]
        rows.append(
            {"code": code, "name": pack.name, "blurb": pack.blurb, "allowances": pack.allowances,
             "price_paise": q["amount_paise"], "gst_paise": tax["gst"], "total_paise": tax["total"]}
        )
    q = quote(list(CODES))
    tax = wallet.gst_split(q["amount_paise"], buyer_state, seller)
    return {
        "packs": rows,
        "bundle": {
            "items": list(CODES), "name": "All three packs", "discount_percent": bundle_discount_percent(),
            "list_price_paise": q["list_price_paise"], "discount_paise": q["discount_paise"],
            "price_paise": q["amount_paise"], "gst_paise": tax["gst"], "total_paise": tax["total"],
        },
        "period_days": PERIOD_DAYS,
        "gst_percent": wallet.GST_PERCENT,
    }


# ---- what a workspace holds now -------------------------------------------------------------


def _when(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def active(tenant: Tenant, now: datetime | None = None) -> dict[str, dict]:
    """Packs whose period has not ended, with their start and end as datetimes."""
    now = now or datetime.now(UTC)
    out: dict[str, dict] = {}
    for code, entry in (tenant.pack_state or {}).items():
        if code not in CODES or not isinstance(entry, dict):
            continue
        start, end = _when(entry.get("start")), _when(entry.get("end"))
        if start and end and end > now:
            out[code] = {**entry, "start": start, "end": end}
    return out


def tier_for(tenant: Tenant, now: datetime | None = None) -> plans.Tier:
    """The plans.Tier a "packs" workspace gets right now, built from the packs that have not expired."""
    import dataclasses

    held = active(tenant, now)
    changes: dict = {}
    if held:
        changes.update(_ANY_PACK_PERKS)
    if "call" in held:
        changes.update(_CALL_PERKS)
        changes["ai_minutes"] = int(held["call"].get("ai_minutes", 0))
    if "whatsapp" in held:
        changes["whatsapp_messages"] = int(held["whatsapp"].get("whatsapp_messages", 0))
    if "email_sms" in held:
        changes["email_messages"] = int(held["email_sms"].get("email_messages", 0))
    return dataclasses.replace(plans.PACKS, **changes) if changes else plans.PACKS


def _need(pack: str, what: str) -> plans.PlanLimitError:
    return plans.PlanLimitError(
        "pack_required",
        f"{what} needs the {CATALOGUE[pack].name}. Buy it under Billing > Packs.",
        tier="packs",
    )


def _used_up(code: str, pack: str, what: str) -> plans.PlanLimitError:
    return plans.PlanLimitError(
        code, f"Your {CATALOGUE[pack].name} {what} are used up. Renew or add to it under Billing > Packs.", tier="packs"
    )


async def minutes_remaining(db: AsyncSession, tenant: Tenant) -> int:
    held = active(tenant).get("call")
    if held is None:
        return 0
    used = await plans.billable_minutes_between(db, tenant.id, held["start"], held["end"])
    return max(int(held.get("ai_minutes", 0)) - used, 0)


async def ensure_minutes(db: AsyncSession, tenant: Tenant) -> None:
    if "call" not in active(tenant):
        raise _need("call", "AI calls")
    if await minutes_remaining(db, tenant) <= 0:
        raise _used_up("pack_minutes_exhausted", "call", "minutes")


async def ensure_whatsapp(db: AsyncSession, tenant: Tenant, count: int = 1) -> None:
    held = active(tenant).get("whatsapp")
    if held is None:
        raise _need("whatsapp", "WhatsApp messages")
    used = int((await aggregate_usage(db, tenant.id, held["start"], held["end"]))["whatsapp_messages"])
    if used + count > int(held.get("whatsapp_messages", 0)):
        raise _used_up("pack_whatsapp_exhausted", "whatsapp", "WhatsApp messages")


def ensure_sms(tenant: Tenant) -> None:
    """No pack includes SMS. A workspace that sends texts is on pay as you go, where each one is paid from the wallet."""
    raise plans.PlanLimitError(
        "sms_not_in_packs",
        "SMS is not part of the channel packs. Switch to pay as you go under Billing to send texts.",
        tier="packs",
    )


async def ensure_email(db: AsyncSession, tenant: Tenant) -> None:
    held = active(tenant).get("email_sms")
    if held is None:
        raise _need("email_sms", "Customer email")
    used = await plans._usage_quantity(db, tenant.id, "email", held["start"], held["end"])
    if used + 1 > int(held.get("email_messages", 0)):
        raise _used_up("pack_email_exhausted", "email_sms", "emails")


async def entitlements(db: AsyncSession, tenant: Tenant, tier: plans.Tier, overage_paise: int) -> dict:
    """The Billing page snapshot for a packs workspace: one meter per channel inside that pack's window."""
    held = active(tenant)
    meters: list[dict] = []

    def meter(key, label, used, limit, pack, unit):
        entry = held.get(pack)
        m = plans._meter(key, label, used, limit if entry else 0, "pack", unit)
        m["pack"], m["expires_at"] = pack, entry["end"].isoformat() if entry else None
        return m

    call, wa, mail = held.get("call"), held.get("whatsapp"), held.get("email_sms")
    meters.append(meter("ai_minutes", "AI call minutes",
                        await plans.billable_minutes_between(db, tenant.id, call["start"], call["end"]) if call else 0,
                        tier.ai_minutes, "call", "min"))
    meters.append(meter("whatsapp_messages", "WhatsApp messages",
                        int((await aggregate_usage(db, tenant.id, wa["start"], wa["end"]))["whatsapp_messages"]) if wa else 0,
                        tier.whatsapp_messages, "whatsapp", "msg"))
    meters.append(meter("email", "Emails",
                        await plans._usage_quantity(db, tenant.id, "email", mail["start"], mail["end"]) if mail else 0,
                        tier.email_messages, "email_sms", "email"))
    for key, (label, model, extra) in plans._RESOURCES.items():
        count = await plans._count(db, model, tenant.id, *extra)
        meters.append(plans._meter(key, label.capitalize(), count, getattr(tier, key), "total", "count"))
    return {
        "tier": "packs", "name": tier.name, "is_free": False,
        "max_call_seconds": tier.max_call_seconds, "max_concurrent_calls": tier.max_concurrent_calls,
        "daily_outbound_calls": tier.daily_outbound_calls, "campaigns": tier.campaigns, "recording": tier.recording,
        "allow_overage": False, "overage_paise": overage_paise, "meters": meters, "price_month_inr": 0,
        "packs": {code: {"expires_at": e["end"].isoformat(), "started_at": e["start"].isoformat()} for code, e in held.items()},
    }


# ---- buying ---------------------------------------------------------------------------------


def can_buy(tenant: Tenant) -> str | None:
    """None when the workspace may buy packs, otherwise the reason in plain words."""
    if (tenant.plan_tier or plans.LEGACY) in BUYABLE_FROM:
        return None
    return {
        "legacy": "Your workspace has unlimited legacy access. Contact support to move to packs.",
        "payg": "You are on pay-as-you-go. Packs and the wallet are separate; contact support to switch.",
    }.get(tenant.plan_tier, f"You are on the {plans.effective(tenant).name} plan. Cancel it before buying packs.")


async def too_many_open_orders(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    since = datetime.now(UTC) - timedelta(hours=1)
    count = (
        await db.execute(
            select(func.count(PackPurchase.id)).where(
                PackPurchase.tenant_id == tenant_id, PackPurchase.status == "created", PackPurchase.created_at >= since
            )
        )
    ).scalar_one()
    return count >= MAX_OPEN_ORDERS_PER_HOUR


async def _next_invoice_number(db: AsyncSession, when: datetime) -> str:
    fy = wallet.financial_year(when)
    # One at a time per financial year, so two payments cannot take the same number. Packs have their own series.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"pack-invoice-{fy}"})
    last = (
        await db.execute(select(func.count(PackPurchase.id)).where(PackPurchase.invoice_number.like(f"CPK/{fy}/%")))
    ).scalar_one()
    return f"CPK/{fy}/{last + 1:06d}"


async def credit_purchase(db: AsyncSession, purchase: PackPurchase, payment_id: str) -> bool:
    """Gives the workspace the packs it paid for, once. Returns False if this order was already applied
    (a webhook after the browser confirmed, or a retry). The caller has already checked the provider's proof
    of payment and that the amount paid equals purchase.total_paise."""
    purchase = (
        await db.execute(select(PackPurchase).where(PackPurchase.id == purchase.id).with_for_update())
    ).scalar_one()
    if purchase.status == "paid":
        return False
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == purchase.tenant_id).with_for_update())
    ).scalar_one()
    now = datetime.now(UTC)
    state = {k: dict(v) for k, v in (tenant.pack_state or {}).items() if isinstance(v, dict)}
    held = active(tenant, now)
    for code in purchase.items:
        grant = purchase.allowances.get(code, {})
        if code in held:  # renewing early: the period runs on from where it ends, the allowance adds up
            entry = state[code]
            entry["end"] = (held[code]["end"] + timedelta(days=purchase.period_days)).isoformat()
            for key, value in grant.items():
                entry[key] = int(entry.get(key, 0)) + int(value)
        else:
            state[code] = {
                "start": now.isoformat(),
                "end": (now + timedelta(days=purchase.period_days)).isoformat(),
                **{k: int(v) for k, v in grant.items()},
            }
    tenant.pack_state = state  # a new dict, so the change is seen and saved
    if (tenant.plan_tier or plans.LEGACY) in BUYABLE_FROM:
        tenant.plan_tier = "packs"
    purchase.status, purchase.provider_payment_id, purchase.paid_at = "paid", payment_id, now
    purchase.invoice_number = await _next_invoice_number(db, now)
    await db.flush()
    return True


async def credit_paid_order(
    db: AsyncSession, *, order_id: str, payment_id: str, paid_paise: int, provider: str
) -> PackPurchase | None:
    """Credits one pack order we created, for the exact amount we asked that provider for. Returns the purchase
    only when this call applied it."""
    purchase = (
        await db.execute(select(PackPurchase).where(PackPurchase.provider_order_id == order_id))
    ).scalar_one_or_none()
    if purchase is None or purchase.provider != provider or paid_paise != purchase.total_paise or not payment_id:
        return None
    return purchase if await credit_purchase(db, purchase, payment_id) else None


# ---- invoice --------------------------------------------------------------------------------


def invoice_html(purchase: PackPurchase, seller: dict) -> str:
    e = html.escape

    def money(paise: int) -> str:
        return wallet.rupees(paise)

    lines = "".join(
        f"<tr><td>{e(CATALOGUE[code].name)} ({purchase.period_days} days)</td>"
        f"<td class=r>{money(CATALOGUE[code].price_inr * 100)}</td></tr>"
        for code in purchase.items if code in CATALOGUE
    )
    if purchase.discount_paise:
        lines += f"<tr><td>Bundle discount</td><td class=r>-{money(purchase.discount_paise)}</td></tr>"
    taxes = (
        [("IGST 18%", purchase.igst_paise)]
        if purchase.igst_paise
        else [("CGST 9%", purchase.cgst_paise), ("SGST 9%", purchase.sgst_paise)]
    )
    tax_rows = "".join(f"<tr><td>{e(n)}</td><td class=r>{money(v)}</td></tr>" for n, v in taxes)
    paid = purchase.paid_at.strftime("%d %b %Y") if purchase.paid_at else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Invoice {e(purchase.invoice_number or '')}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;color:#0b1b2b;max-width:680px;margin:32px auto;padding:0 20px}}
h1{{font-size:20px;margin:0 0 4px}}table{{width:100%;border-collapse:collapse;margin:16px 0}}td,th{{padding:8px 6px;border-bottom:1px solid #dbe9e4;text-align:left}}
.r{{text-align:right}}.muted{{color:#475569}}.tot td{{font-weight:600;border-bottom:0}}</style></head><body>
<h1>Tax invoice {e(purchase.invoice_number or '')}</h1><p class=muted>Date {e(paid)}</p>
<p><strong>{e(seller.get('name') or '')}</strong><br>{e(seller.get('address') or '')}<br>GSTIN {e(seller.get('gstin') or '—')}</p>
<p><strong>Billed to</strong><br>{e(purchase.billing_name or '')}<br>GSTIN {e(purchase.gstin or '—')} · State code {e(purchase.state_code or '—')}</p>
<table><tr><th>Description</th><th class=r>Amount</th></tr>{lines}
<tr><td>Taxable value</td><td class=r>{money(purchase.amount_paise)}</td></tr>{tax_rows}
<tr class=tot><td>Total paid</td><td class=r>{money(purchase.total_paise)}</td></tr></table>
{wallet._paypal_collected(purchase)}
<p class=muted>Payment {e(purchase.provider_payment_id or '')}. Each pack is valid for {purchase.period_days} days from the day it starts. Unused allowance does not carry over.</p>
</body></html>"""
