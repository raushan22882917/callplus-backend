import uuid

from app.tools.context import ToolContext
from app.tools.handlers import add_customer_note, create_ticket, get_customer
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, Tenant


async def _make_tenant_and_customer(
    db: AsyncSession, *, phone: str = "+15550002222"
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Jane", phone=phone)
    db.add(customer)
    await db.flush()
    await db.commit()
    return tenant.id, customer.id


def _ctx(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID | None) -> ToolContext:
    return ToolContext(
        db=db,
        tenant_id=tenant_id,
        agent_id=None,
        call_id=None,
        conversation_id=None,
        customer_id=customer_id,
    )


async def test_get_customer_by_phone_is_tenant_scoped(db_session: AsyncSession) -> None:
    tenant_a, customer_a = await _make_tenant_and_customer(db_session, phone="+15550002222")
    tenant_b, _ = await _make_tenant_and_customer(db_session, phone="+15559998888")

    found_in_a = await get_customer(_ctx(db_session, tenant_a, None), phone="+15550002222")
    assert found_in_a["found"] is True
    assert found_in_a["customer_id"] == str(customer_a)

    # Tenant B has no customer with A's phone number — must not see it.
    not_found_in_b = await get_customer(_ctx(db_session, tenant_b, None), phone="+15550002222")
    assert not_found_in_b["found"] is False


async def test_create_ticket_and_add_note_use_conversation_customer_by_default(
    db_session: AsyncSession,
) -> None:
    tenant_id, customer_id = await _make_tenant_and_customer(db_session)
    ctx = _ctx(db_session, tenant_id, customer_id)

    result = await create_ticket(ctx, subject="Billing question", priority="high")
    assert "ticket_id" in result

    note_result = await add_customer_note(ctx, note="Called about billing")
    assert note_result == {"recorded": True}


async def test_create_ticket_without_customer_context_errors(db_session: AsyncSession) -> None:
    tenant_id, _ = await _make_tenant_and_customer(db_session)
    ctx = _ctx(db_session, tenant_id, None)

    result = await create_ticket(ctx, subject="No customer")
    assert "error" in result
