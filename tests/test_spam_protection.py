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


async def _setup_agent_and_number(client: AsyncClient, tokens: dict, number: str) -> None:
    agent_resp = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Receptionist"}
    )
    agent_id = agent_resp.json()["id"]
    resp = await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": number, "ai_agent_id": agent_id},
    )
    assert resp.status_code == 201, resp.text


async def test_spam_protection_auto_blocks_after_threshold(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="spam-co", email="owner@spamco.com")
    patch_resp = await client.patch(
        "/api/v1/tenants/me",
        headers=_auth(tokens),
        json={"spam_call_threshold": 2, "spam_call_window_minutes": 60},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    await _setup_agent_and_number(client, tokens, "+15559990000")

    call_data = {"To": "+15559990000", "From": "+15551230000"}

    # First two calls (== threshold) go through normally.
    for i in range(2):
        resp = await client.post(
            "/api/v1/webhooks/telephony/twilio/voice", data={**call_data, "CallSid": f"CAspam{i}"}
        )
        assert resp.status_code == 200
        assert "<Stream" in resp.text

    # The third call from the same number crosses the threshold: rejected
    # and the caller is auto-added to the blocklist.
    resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice", data={**call_data, "CallSid": "CAspam2"}
    )
    assert resp.status_code == 200
    assert "<Reject" in resp.text

    blocked_resp = await client.get("/api/v1/blocked-callers", headers=_auth(tokens))
    blocked = blocked_resp.json()
    assert len(blocked) == 1
    assert blocked[0]["e164_number"] == "+15551230000"
    assert "auto" in blocked[0]["reason"]

    # A subsequent call is rejected via the blocklist, not the threshold check.
    resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice", data={**call_data, "CallSid": "CAspam3"}
    )
    assert "<Reject" in resp.text


async def test_spam_protection_disabled_never_blocks(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="no-spam-co", email="owner@nospamco.com")
    await client.patch(
        "/api/v1/tenants/me",
        headers=_auth(tokens),
        json={"spam_protection_enabled": False, "spam_call_threshold": 1},
    )
    await _setup_agent_and_number(client, tokens, "+15559993333")

    call_data = {"To": "+15559993333", "From": "+15551239999"}
    for i in range(3):
        resp = await client.post(
            "/api/v1/webhooks/telephony/twilio/voice", data={**call_data, "CallSid": f"CAns{i}"}
        )
        assert "<Stream" in resp.text

    blocked_resp = await client.get("/api/v1/blocked-callers", headers=_auth(tokens))
    assert blocked_resp.json() == []


async def test_manual_block_and_unblock(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="manual-block-co", email="owner@manualblock.com")
    await _setup_agent_and_number(client, tokens, "+15559991111")

    block_resp = await client.post(
        "/api/v1/blocked-callers", headers=_auth(tokens), json={"e164_number": "+15559992222"}
    )
    assert block_resp.status_code == 201, block_resp.text
    blocked_id = block_resp.json()["id"]

    rejected_resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15559991111", "From": "+15559992222", "CallSid": "CAblocked1"},
    )
    assert "<Reject" in rejected_resp.text

    unblock_resp = await client.delete(
        f"/api/v1/blocked-callers/{blocked_id}", headers=_auth(tokens)
    )
    assert unblock_resp.status_code == 204

    allowed_resp = await client.post(
        "/api/v1/webhooks/telephony/twilio/voice",
        data={"To": "+15559991111", "From": "+15559992222", "CallSid": "CAblocked2"},
    )
    assert "<Stream" in allowed_resp.text


async def test_another_tenant_cannot_see_or_remove_blocked_callers(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="block-tenant-a", email="owner@blocktenanta.com")
    tokens_b = await _signup(client, slug="block-tenant-b", email="owner@blocktenantb.com")

    block_resp = await client.post(
        "/api/v1/blocked-callers", headers=_auth(tokens_a), json={"e164_number": "+15551110000"}
    )
    blocked_id = block_resp.json()["id"]

    list_as_b = await client.get("/api/v1/blocked-callers", headers=_auth(tokens_b))
    assert list_as_b.json() == []

    delete_as_b = await client.delete(
        f"/api/v1/blocked-callers/{blocked_id}", headers=_auth(tokens_b)
    )
    assert delete_as_b.status_code == 404
