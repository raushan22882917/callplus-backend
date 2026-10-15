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


async def test_tenant_cannot_read_another_tenants_customer(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="tenant-a", email="owner@tenant-a.com")
    tokens_b = await _signup(client, slug="tenant-b", email="owner@tenant-b.com")

    create_resp = await client.post(
        "/api/v1/customers", headers=_auth(tokens_a), json={"name": "Jane Doe"}
    )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()["id"]

    list_as_b = await client.get("/api/v1/customers", headers=_auth(tokens_b))
    assert list_as_b.json() == []

    get_as_b = await client.get(f"/api/v1/customers/{customer_id}", headers=_auth(tokens_b))
    assert get_as_b.status_code == 404

    get_as_a = await client.get(f"/api/v1/customers/{customer_id}", headers=_auth(tokens_a))
    assert get_as_a.status_code == 200
    assert get_as_a.json()["id"] == customer_id


async def test_agent_role_cannot_manage_settings_or_create_customers(client: AsyncClient) -> None:
    owner_tokens = await _signup(client, slug="clinic", email="owner@clinic.com")

    invite_resp = await client.post(
        "/api/v1/users/invite",
        headers=_auth(owner_tokens),
        json={"email": "agent@clinic.com", "full_name": "Agent Smith", "role_name": "AGENT"},
    )
    assert invite_resp.status_code == 201
    temp_password = invite_resp.json()["temporary_password"]

    login_resp = await client.post(
        "/api/v1/auth/login", json={"email": "agent@clinic.com", "password": temp_password}
    )
    agent_tokens = login_resp.json()["tokens"]

    settings_resp = await client.patch(
        "/api/v1/tenants/me", headers=_auth(agent_tokens), json={"name": "Hacked"}
    )
    assert settings_resp.status_code == 403

    create_resp = await client.post(
        "/api/v1/customers", headers=_auth(agent_tokens), json={"name": "Should fail"}
    )
    assert create_resp.status_code == 403

    read_resp = await client.get("/api/v1/customers", headers=_auth(agent_tokens))
    assert read_resp.status_code == 200


async def test_unauthenticated_request_is_rejected(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/customers")
    assert resp.status_code == 401
