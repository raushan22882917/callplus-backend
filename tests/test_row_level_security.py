import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, Tenant
from packages.database.rls import apply_rls, remove_rls
from packages.database.session import get_engine, session_scope, set_tenant_context


@pytest.fixture
async def rls_enabled() -> None:
    async with get_engine().begin() as conn:
        # Idempotent: under `TEST_RLS=1` conftest has already applied it.
        await conn.run_sync(remove_rls)
        tables = await conn.run_sync(apply_rls)
    assert "customers" in tables and "tenants" in tables
    # No teardown needed: the autouse _reset_database fixture drops and
    # recreates every table (policies included) before the next test.


async def _two_tenants_with_customers(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenants = [Tenant(name=n, slug=n.lower()) for n in ("Alpha", "Beta")]
    db.add_all(tenants)
    await db.flush()
    for tenant in tenants:
        db.add(Customer(tenant_id=tenant.id, name=f"cust-{tenant.slug}", phone=f"+1{tenant.slug}"))
    await db.commit()
    return tenants[0].id, tenants[1].id


async def test_database_hides_other_tenants_rows_even_without_a_where_clause(
    db_session: AsyncSession, rls_enabled: None
) -> None:
    # No tenant context (signup/webhook/worker paths): everything is visible.
    alpha, beta = await _two_tenants_with_customers(db_session)
    assert (await db_session.execute(text("SELECT count(*) FROM customers"))).scalar_one() == 2

    # A tenant-scoped session sees only its own rows from a deliberately
    # unfiltered query — the bug RLS exists to contain.
    async with session_scope() as scoped:
        await set_tenant_context(scoped, alpha)
        names = (await scoped.execute(text("SELECT name FROM customers"))).scalars().all()
        assert names == ["cust-alpha"]
        assert (await scoped.execute(text("SELECT count(*) FROM tenants"))).scalar_one() == 1

        # The scope survives commits (each new transaction re-applies it).
        await scoped.commit()
        names = (await scoped.execute(text("SELECT name FROM customers"))).scalars().all()
        assert names == ["cust-alpha"]


async def test_tenant_scoped_session_cannot_write_other_tenants_rows(
    db_session: AsyncSession, rls_enabled: None
) -> None:
    alpha, beta = await _two_tenants_with_customers(db_session)
    async with session_scope() as scoped:
        await set_tenant_context(scoped, alpha)

        # Inserting a row for another tenant is rejected outright...
        scoped.add(Customer(tenant_id=beta, name="smuggled", phone="+1x"))
        with pytest.raises(DBAPIError):
            await scoped.flush()
        await scoped.rollback()

        # ...and UPDATE/DELETE silently match nothing.
        updated = await scoped.execute(text("UPDATE customers SET name = 'pwned'"))
        assert updated.rowcount == 1  # only Alpha's own row
        await scoped.rollback()

    names = (await db_session.execute(text("SELECT name FROM customers ORDER BY name"))).scalars()
    assert list(names) == ["cust-alpha", "cust-beta"]


async def test_api_requests_are_confined_to_the_callers_tenant(
    client: AsyncClient, rls_enabled: None
) -> None:
    async def signup(slug: str) -> dict:
        resp = await client.post(
            "/api/v1/auth/signup",
            json={
                "company_name": slug,
                "tenant_slug": slug,
                "full_name": "Owner",
                "email": f"owner@{slug}.com",
                "password": "supersecret1",
            },
        )
        assert resp.status_code == 201, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    a, b = await signup("rls-a"), await signup("rls-b")
    created = await client.post(
        "/api/v1/customers", headers=a, json={"name": "Jane", "phone": "+15550001111"}
    )
    assert created.status_code == 201, created.text
    assert len((await client.get("/api/v1/customers", headers=a)).json()) == 1
    assert (await client.get("/api/v1/customers", headers=b)).json() == []
