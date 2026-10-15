"""Shared "find or create a Customer by phone number" — used by every
inbound-by-phone entry point (an inbound call, an outbound call to a new
number, a WhatsApp message) so a caller only ever gets one Customer row no
matter which channel first saw their number.

Doesn't commit or fire CUSTOMER_CREATED itself — callers do that after their
own commit, the same convention app/services/call_finalize.py uses for
CALL_ENDED, so a workflow picking this up from its own DB session never
races the creating transaction.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer


def normalize_phone(phone: str) -> str:
    """One format for every channel: '+' and digits. Twilio sends '+919…',
    WhatsApp sends '919…' (no plus); without this the same person became two
    customers, and a call never saw what they'd said on WhatsApp."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if digits else raw


async def find_or_create_customer_by_phone(
    db: AsyncSession, tenant_id: uuid.UUID, phone: str
) -> tuple[Customer, bool]:
    """Returns (customer, created)."""
    normalized = normalize_phone(phone)
    variants = {normalized, normalized.lstrip("+"), (phone or "").strip()} - {""}
    rows = (
        await db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.phone.in_(variants)))
    ).scalars().all()
    if rows:
        customer = next((c for c in rows if c.phone == normalized), rows[0])
        if customer.phone != normalized:
            if customer.name == customer.phone:
                customer.name = normalized
            customer.phone = normalized  # records saved without the '+' join the rest
        return customer, False

    customer = Customer(tenant_id=tenant_id, name=normalized, phone=normalized)
    db.add(customer)
    await db.flush()
    return customer, True
