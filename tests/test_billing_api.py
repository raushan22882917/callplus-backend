from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import BillingInterval, Plan


async def _signup(client: AsyncClient, *, slug: str, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": email,
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_list_plans(client: AsyncClient, db_session: AsyncSession) -> None:
    db_session.add(
        Plan(
            code="starter",
            name="Starter",
            price_paise=490000,
            currency="INR",
            billing_interval=BillingInterval.MONTH,
            is_active=True,
        )
    )
    await db_session.commit()

    tokens = await _signup(client, slug="billing-co", email="owner@billing-co.com")
    resp = await client.get("/api/v1/billing/plans", headers=_auth(tokens))
    assert resp.status_code == 200, resp.text
    codes = [p["code"] for p in resp.json()]
    assert "starter" in codes


async def test_get_subscription_404_when_none_exists(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="no-sub-co", email="owner@no-sub-co.com")
    resp = await client.get("/api/v1/billing/subscription", headers=_auth(tokens))
    assert resp.status_code == 404


async def test_create_subscription_fails_gracefully_without_razorpay_keys(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        Plan(
            code="growth",
            name="Growth",
            price_paise=1990000,
            currency="INR",
            billing_interval=BillingInterval.MONTH,
            provider_plan_id="plan_fake123",
            is_active=True,
        )
    )
    await db_session.commit()

    tokens = await _signup(client, slug="no-keys-co", email="owner@no-keys-co.com")
    # conftest blanks RAZORPAY_KEY_ID/SECRET for the whole test run, so this
    # must fail as a clean 424 (upstream not configured), never a 500.
    resp = await client.post(
        "/api/v1/billing/subscription", headers=_auth(tokens), json={"plan_code": "growth"}
    )
    assert resp.status_code == 424, resp.text


async def test_create_subscription_rejects_plan_without_provider_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(
        Plan(
            code="scale",
            name="Scale",
            price_paise=4990000,
            currency="INR",
            billing_interval=BillingInterval.MONTH,
            is_active=True,
        )
    )
    await db_session.commit()

    tokens = await _signup(client, slug="unwired-co", email="owner@unwired-co.com")
    resp = await client.post(
        "/api/v1/billing/subscription", headers=_auth(tokens), json={"plan_code": "scale"}
    )
    assert resp.status_code == 424
