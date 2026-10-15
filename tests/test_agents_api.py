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


async def test_create_agent_assign_phone_number_and_enable_tool(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="dental-clinic", email="owner@dentalclinic.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents",
        headers=_auth(tokens),
        json={"name": "Receptionist", "greeting": "Thanks for calling!"},
    )
    assert agent_resp.status_code == 201, agent_resp.text
    agent = agent_resp.json()
    assert agent["status"] == "draft"

    phone_resp = await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": "+15551234567", "ai_agent_id": agent["id"]},
    )
    assert phone_resp.status_code == 201, phone_resp.text
    assert phone_resp.json()["ai_agent_id"] == agent["id"]

    tool_resp = await client.put(
        f"/api/v1/ai-agents/{agent['id']}/tools",
        headers=_auth(tokens),
        json={"tool_name": "create_ticket", "enabled": True},
    )
    assert tool_resp.status_code == 200, tool_resp.text
    assert tool_resp.json()["enabled"] is True

    tools_list = await client.get(f"/api/v1/ai-agents/{agent['id']}/tools", headers=_auth(tokens))
    assert len(tools_list.json()) == 1

    bad_tool_resp = await client.put(
        f"/api/v1/ai-agents/{agent['id']}/tools",
        headers=_auth(tokens),
        json={"tool_name": "not_a_real_tool", "enabled": True},
    )
    assert bad_tool_resp.status_code == 400


async def test_another_tenant_cannot_see_agent(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="clinic-a", email="owner@clinic-a.com")
    tokens_b = await _signup(client, slug="clinic-b", email="owner@clinic-b.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens_a), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]

    get_as_b = await client.get(f"/api/v1/ai-agents/{agent_id}", headers=_auth(tokens_b))
    assert get_as_b.status_code == 404
