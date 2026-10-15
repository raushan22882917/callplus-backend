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


async def test_customer_update_and_delete(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="crud-co", email="owner@crud-co.com")

    create_resp = await client.post(
        "/api/v1/customers", headers=_auth(tokens), json={"name": "Original Name"}
    )
    customer_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/customers/{customer_id}", headers=_auth(tokens), json={"name": "Updated Name"}
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["name"] == "Updated Name"

    delete_resp = await client.delete(f"/api/v1/customers/{customer_id}", headers=_auth(tokens))
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/customers/{customer_id}", headers=_auth(tokens))
    assert get_resp.status_code == 404


async def test_another_tenant_cannot_update_or_delete_customer(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="crud-a", email="owner@crud-a.com")
    tokens_b = await _signup(client, slug="crud-b", email="owner@crud-b.com")

    create_resp = await client.post(
        "/api/v1/customers", headers=_auth(tokens_a), json={"name": "A's Customer"}
    )
    customer_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/customers/{customer_id}", headers=_auth(tokens_b), json={"name": "Hijacked"}
    )
    assert patch_resp.status_code == 404

    delete_resp = await client.delete(f"/api/v1/customers/{customer_id}", headers=_auth(tokens_b))
    assert delete_resp.status_code == 404


async def test_campaign_pause_and_resume_guard_status(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="pause-co", email="owner@pause-co.com")

    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    assert agent_resp.status_code == 201, agent_resp.text

    campaign_resp = await client.post(
        "/api/v1/campaigns",
        headers=_auth(tokens),
        json={"name": "Spring Promo", "channel": "voice", "ai_agent_id": agent_resp.json()["id"]},
    )
    assert campaign_resp.status_code == 201, campaign_resp.text
    campaign_id = campaign_resp.json()["id"]

    # Can't pause a draft campaign.
    early_pause = await client.post(f"/api/v1/campaigns/{campaign_id}/pause", headers=_auth(tokens))
    assert early_pause.status_code == 409

    launch_resp = await client.post(
        f"/api/v1/campaigns/{campaign_id}/launch", headers=_auth(tokens)
    )
    assert launch_resp.status_code == 200, launch_resp.text

    pause_resp = await client.post(f"/api/v1/campaigns/{campaign_id}/pause", headers=_auth(tokens))
    assert pause_resp.status_code == 200, pause_resp.text
    assert pause_resp.json()["status"] == "paused"

    # Can't pause an already-paused campaign.
    double_pause = await client.post(
        f"/api/v1/campaigns/{campaign_id}/pause", headers=_auth(tokens)
    )
    assert double_pause.status_code == 409


async def test_user_membership_update_blocks_removing_last_owner(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="owner-guard-co", email="owner@owner-guard-co.com")

    me_resp = await client.get("/api/v1/users/me", headers=_auth(tokens))
    membership_id = None
    members_resp = await client.get("/api/v1/users", headers=_auth(tokens))
    assert members_resp.status_code == 200
    for member in members_resp.json():
        if member["user_id"] == me_resp.json()["user_id"]:
            membership_id = member["membership_id"]
    assert membership_id is not None

    resp = await client.patch(
        f"/api/v1/users/{membership_id}", headers=_auth(tokens), json={"role_name": "AGENT"}
    )
    assert resp.status_code == 409
