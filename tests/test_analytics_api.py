from httpx import AsyncClient


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


async def test_overview_counts_customers_and_tickets(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="analytics-co", email="owner@analytics-co.com")

    customer_resp = await client.post(
        "/api/v1/customers",
        headers=_auth(tokens),
        json={"name": "Jane Doe", "phone": "+15551230000"},
    )
    assert customer_resp.status_code == 201, customer_resp.text
    customer_id = customer_resp.json()["id"]

    for subject in ("Billing question", "Cannot log in"):
        ticket_resp = await client.post(
            "/api/v1/tickets",
            headers=_auth(tokens),
            json={"customer_id": customer_id, "subject": subject},
        )
        assert ticket_resp.status_code == 201, ticket_resp.text

    overview = await client.get("/api/v1/analytics/overview", headers=_auth(tokens))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["customers_count"] == 1
    assert body["tickets_total"] == 2
    assert body["open_tickets"] == 2
    assert body["tickets_by_status"]["open"] == 2


async def test_another_tenant_does_not_see_this_tenants_data(client: AsyncClient) -> None:
    tokens_a = await _signup(
        client, slug="tenant-a-analytics", email="owner@tenant-a-analytics.com"
    )
    tokens_b = await _signup(
        client, slug="tenant-b-analytics", email="owner@tenant-b-analytics.com"
    )

    await client.post(
        "/api/v1/customers", headers=_auth(tokens_a), json={"name": "A's Customer"}
    )

    overview_b = await client.get("/api/v1/analytics/overview", headers=_auth(tokens_b))
    assert overview_b.status_code == 200
    assert overview_b.json()["customers_count"] == 0


async def test_tickets_and_campaigns_analytics_endpoints(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="analytics-co-2", email="owner@analytics-co-2.com")

    tickets_resp = await client.get("/api/v1/analytics/tickets", headers=_auth(tokens))
    assert tickets_resp.status_code == 200
    assert tickets_resp.json()["by_status"] == {}

    campaigns_resp = await client.get("/api/v1/analytics/campaigns", headers=_auth(tokens))
    assert campaigns_resp.status_code == 200
    assert campaigns_resp.json()["by_status"] == {}
