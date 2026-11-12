import uuid

import pytest
from app.core.config import get_settings
from app.integrations.credentials import SharedAccountError, get_sarvam_api_key, require_own_twilio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant


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


async def test_the_platform_sarvam_key_is_used_only_when_the_platform_switches_it_on(
    db_session: AsyncSession, monkeypatch
) -> None:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.commit()

    # No platform key set and no key of its own: the workspace gets nothing, never a shared account.
    assert await get_sarvam_api_key(db_session, tenant.id) == ""
    # Setting SARVAM_API_KEY is the platform switching CallPlus's Sarvam account on for workspaces.
    monkeypatch.setattr(get_settings(), "sarvam_api_key", "platform-key")
    assert await get_sarvam_api_key(db_session, tenant.id) == "platform-key"
    monkeypatch.setattr(get_settings(), "twilio_account_sid", "ACplatform")
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "platform-token")
    with pytest.raises(SharedAccountError):
        await require_own_twilio(db_session, tenant.id)


async def test_connect_twilio_then_credentials_resolve_to_tenants_own(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="byo-twilio-co", email="owner@byotwilio.com")

    connect_resp = await client.post(
        "/api/v1/integrations/twilio",
        headers=_auth(tokens),
        json={
            "account_sid": "ACtenantowned000000000000000000",
            "auth_token": "tenant-secret-token",
        },
    )
    assert connect_resp.status_code == 201, connect_resp.text
    body = connect_resp.json()
    assert body["provider"] == "twilio"
    assert body["config"]["account_sid"] == "ACtenantowned000000000000000000"
    assert "auth_token" not in body  # never echo the secret back

    list_resp = await client.get("/api/v1/integrations", headers=_auth(tokens))
    assert len(list_resp.json()) == 1

    disconnect_resp = await client.delete("/api/v1/integrations/twilio", headers=_auth(tokens))
    assert disconnect_resp.status_code == 204

    list_after = await client.get("/api/v1/integrations", headers=_auth(tokens))
    assert list_after.json() == []


async def test_connect_sarvam_key_is_tenant_isolated(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="byo-sarvam-a", email="owner@byosarvam-a.com")
    tokens_b = await _signup(client, slug="byo-sarvam-b", email="owner@byosarvam-b.com")

    await client.post(
        "/api/v1/integrations/sarvam", headers=_auth(tokens_a), json={"api_key": "tenant-a-key"}
    )

    # Tenant B never connected anything — must not see tenant A's integration.
    list_as_b = await client.get("/api/v1/integrations", headers=_auth(tokens_b))
    assert list_as_b.json() == []


async def test_provision_phone_number_without_twilio_configured_fails_cleanly(
    client: AsyncClient,
) -> None:
    settings = get_settings()
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return  # a real Twilio account is configured — this would actually buy a number

    tokens = await _signup(client, slug="no-twilio-co", email="owner@notwilio.com")

    resp = await client.post(
        "/api/v1/phone-numbers/provision",
        headers=_auth(tokens),
        json={"phone_number": "+15551234567"},
    )
    assert resp.status_code == 422
    assert "Exotel" in resp.json()["detail"]


async def test_setup_lists_what_the_workspace_still_has_to_connect(client):
    tokens = await _signup_for_included(client)
    headers = {"Authorization": f"Bearer {tokens}"}
    before = await client.get("/api/v1/integrations/setup", headers=headers)
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["ready_for_calls"] is False
    assert {item["id"]: item["connected"] for item in body["items"]} == {
        "exotel": False,
        "twilio": False,
        "sarvam": False,
        "whatsapp": False,
    }
    await client.post("/api/v1/integrations/sarvam", headers=headers, json={"api_key": "tenant-key"})
    after = (await client.get("/api/v1/integrations/included", headers=headers)).json()
    assert after["sarvam"] is True and after["exotel"] is False and after["twilio"] is False


async def test_included_services_need_a_signed_in_user(client):
    assert (await client.get("/api/v1/integrations/included")).status_code in (401, 403)


async def _signup_for_included(client) -> str:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Inc Co",
            "tenant_slug": "inc-co",
            "full_name": "Owner",
            "email": "owner@inc-co.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


async def test_twilio_details_twilio_refuses_are_not_saved(
    client: AsyncClient, monkeypatch
) -> None:
    tokens = await _signup(client, slug="twilio-check", email="owner@twilio-check.com")
    monkeypatch.setattr(
        "app.api.v1.integrations.check_account",
        lambda *_a, **_k: (False, "Your Twilio account is suspended. Reactivate it."),
    )
    resp = await client.post(
        "/api/v1/integrations/twilio",
        headers=_auth(tokens),
        json={"account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "auth_token": "tok"},
    )
    assert resp.status_code == 422 and "suspended" in resp.json()["detail"]
    listed = await client.get("/api/v1/integrations", headers=_auth(tokens))
    assert [i for i in listed.json() if i["provider"] == "twilio"] == []


async def test_twilio_status_reports_what_twilio_says(client: AsyncClient, monkeypatch) -> None:
    tokens = await _signup(client, slug="twilio-check", email="owner@twilio-check.com")
    monkeypatch.setattr(
        "app.api.v1.integrations.check_account",
        lambda *_a, **_k: (False, "Your Twilio account is closed."),
    )
    resp = await client.get("/api/v1/integrations/twilio/status", headers=_auth(tokens))
    assert resp.status_code == 200 and resp.json() == {
        "ok": False,
        "message": "Your Twilio account is closed.",
    }
